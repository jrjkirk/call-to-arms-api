"""Discord guild (server) membership checks, for the "must be in the club's
Discord to sign up" gate.

Why a bot and not an OAuth scope: the login flow uses the Discord access token
once, at callback, to read the user's identity and then throws it away — the
session cookie is a stateless HMAC of the user id (auth.py). Checking guild
membership via the `guilds` OAuth scope would mean storing and refreshing
per-user access tokens, plus re-prompting every existing user for consent. A
bot token belongs to the app, needs no user consent, and never expires.

Why this needs no privileged intent: we only ever ask about ONE known user in
ONE known guild. Discord gates `GET /guilds/{id}/members` (enumerate everyone)
behind the GUILD_MEMBERS privileged intent, but `GET /guilds/{id}/members/
{user_id}` is not gated — "if you know who you're looking for, you don't need
to enumerate every member". That keeps setup free of Discord app review.

EVERYTHING HERE FAILS OPEN. `is_guild_member` returns None (= "couldn't
determine") for a missing token, a network error, a revoked bot, a rate limit
— anything that isn't a definitive yes/no from Discord. Callers must treat
None as "let them through". Blocking real members from signing up because
Discord had a wobble is far worse than letting the occasional stranger past,
and a silent hard-fail here would break the club's whole signup night.
"""
import os
import re
from typing import Optional

import httpx

from observability import capture

DISCORD_API = "https://discord.com/api/v10"
_TIMEOUT = 5.0

# Any of:
#   https://discord.gg/abc123
#   https://discord.com/invite/abc123
#   https://discordapp.com/invite/abc123
_INVITE_RE = re.compile(
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]+)", re.IGNORECASE
)
# A direct channel URL carries the guild id outright:
#   https://discord.com/channels/<guild_id>/<channel_id>
_CHANNELS_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)", re.IGNORECASE)


def bot_token() -> str:
    """Read lazily so the module imports fine when the secret isn't set —
    same pattern as storage.py's Supabase credentials."""
    return os.environ.get("DISCORD_BOT_TOKEN", "").strip()


def is_configured() -> bool:
    """Whether a bot token exists at all. False means every check no-ops."""
    return bool(bot_token())


def _headers() -> dict:
    return {"Authorization": f"Bot {bot_token()}"}


def is_guild_member(guild_id: str, discord_user_id: str) -> Optional[bool]:
    """True if the user is in the guild, False if definitively not, None if
    undetermined.

    None is the fail-open signal and covers: no bot token, the bot not being
    in that guild (403), an unknown guild, rate limiting, timeouts, and any
    unexpected status. Only a clean 200 (in) or 404 (not in) is treated as an
    answer.
    """
    if not guild_id or not discord_user_id or not is_configured():
        return None

    url = f"{DISCORD_API}/guilds/{guild_id}/members/{discord_user_id}"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=_TIMEOUT)
    except Exception as e:
        capture(e, kind="discord_guild_check", guild_id=guild_id)
        return None

    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        # Discord returns 404 both for "user isn't a member" and for "no such
        # guild". The latter only happens on a misconfigured guild id, which
        # the admin connection test is there to catch — treating it as "not a
        # member" here would wrongly block everyone at that club, so we only
        # trust the 404 when we can see the guild at all.
        if guild_name(guild_id) is None:
            capture(
                RuntimeError(f"Guild {guild_id} not visible to the bot"),
                kind="discord_guild_misconfigured",
                guild_id=guild_id,
            )
            return None
        return False

    # 401 bad token, 403 bot not in guild, 429 rate limited, 5xx Discord down.
    capture(
        RuntimeError(f"Discord guild check returned {resp.status_code}"),
        kind="discord_guild_check",
        guild_id=guild_id,
        status=resp.status_code,
    )
    return None


def guild_name(guild_id: str) -> Optional[str]:
    """The guild's name, or None if the bot can't see it. Used by the admin
    connection test to echo the server name back — so a mistyped id shows up
    as the wrong club rather than as a silent misconfiguration weeks later."""
    if not guild_id or not is_configured():
        return None
    try:
        resp = httpx.get(
            f"{DISCORD_API}/guilds/{guild_id}", headers=_headers(), timeout=_TIMEOUT
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    return resp.json().get("name")


def guild_id_from_url(url: Optional[str]) -> Optional[str]:
    """Pull a guild id straight out of a Discord channel URL, without any API
    call. Returns None for invite links (use resolve_invite_guild_id)."""
    if not url:
        return None
    m = _CHANNELS_RE.search(url)
    return m.group(1) if m else None


def resolve_invite_guild_id(url: Optional[str]) -> Optional[str]:
    """Resolve a Discord invite URL to the guild id it points at, so a club
    that has already pasted its invite link (Club.discord_url, part of the
    onboarding checklist) doesn't have to hunt for a server id at all.

    Needs no bot token — Get Invite is unauthenticated. Returns None if the
    URL isn't an invite, the invite has expired/been revoked, or Discord is
    unreachable; the admin's manual field is the fallback for all of those.
    """
    if not url:
        return None

    direct = guild_id_from_url(url)
    if direct:
        return direct

    m = _INVITE_RE.search(url)
    if not m:
        return None
    code = m.group(1)
    try:
        resp = httpx.get(f"{DISCORD_API}/invites/{code}", timeout=_TIMEOUT)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    guild = resp.json().get("guild") or {}
    return guild.get("id")


def bot_invite_url(client_id: str) -> str:
    """The link a club forwards to whoever runs their Discord.

    permissions=0 deliberately: the bot needs no capabilities in the server,
    only presence, since membership lookups aren't permission-gated. Asking
    for nothing also makes it a far easier "yes" for a server owner who isn't
    part of the club's app admin team.
    """
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={client_id}&scope=bot&permissions=0"
    )
