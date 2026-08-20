"""Signup endpoints: the Call to Arms form.

Semantics are a faithful port of the Streamlit app:
- One effective signup per player/week/system. Submitting again updates the
  newest existing row and deletes any older duplicates.
- Dropping out is blocked once pairings are published for that week/system.
- Dropping out also deletes any PREARRANGED pairing involving the dropper
  (the opponent's signup stays, so they get re-pooled next pairing run).
- Discord webhooks fire on brand-new signups and on drops, per-system,
  and silently no-op when the club has no webhook configured for that
  system (no cross-club fallback — see resolve_webhook_url).
"""
import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, SQLModel, select

from database import active_player_id_for, get_session, name_with_mention, resolve_webhook_url, scoped, system_setting_slug, get_setting
from models import Signup, Pairing, PublishState, Player, User, SystemConfig, ClubSystem, TableBookingConfig, Club, PlayerDiscordVerification, PlayerExperienceAdjustment
import discord_guild
from experience import summary as experience_summary
from levels import progress as level_progress
from auth import active_club_id, admin_scopes, require_user
from systems import SYSTEM_RULES
from observability import capture

router = APIRouter(prefix="/signups", tags=["signups"])

# Values accepted when an ADMIN sets a signup's experience by hand (the pairing
# grid and the add-signup form). Players no longer choose — theirs is derived
# from games played, see experience.py. "Some" is the retired name for the
# middle tier and stays accepted so historical rows and the admin grid's
# existing dropdown keep validating.
EXPERIENCE_OPTIONS = {"New", "Some", "Experienced", "Veteran"}

# Historical snapshot of the pre-catalogue hardcoded per-system values. The
# live request path no longer uses these — the SystemConfig catalogue is the
# source of truth (see submit_signup / pairings_engine). They remain only so
# seed/seed_systems_config.py can cross-check that the original seed matched
# these values; safe to delete once that historical script is retired.
SYSTEMS = set(SYSTEM_RULES)
TOW_VIBES = {"Casual", "Competitive", "Intro", "Either"}
HH_VIBES = {"Standard", "Intro"}
SCENARIO_OPTIONS = {"Open Battle", "Weekly Scenario"}

# Platform-level canonical vibe palette. Per-club vibe configuration
# (ClubSystem.vibe_options) is chosen from this fixed set — "Intro" (drives
# the pairing intro pre-pass) and "Standard" (baseline) are protected members.
CANONICAL_VIBES = ["Casual", "Competitive", "Standard", "Intro", "Either"]

APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")


class SignupIn(SQLModel):
    """Request body for POST /signups. Player identity comes from the session."""
    system: str
    week: str
    faction: Optional[str] = None
    points: Optional[int] = None
    eta: Optional[str] = None
    experience: str = "New"
    vibe: str = "Casual"
    standby_ok: bool = False
    scenario: Optional[str] = None
    can_demo: bool = False


class SwapIn(SQLModel):
    """Request body for POST /signups/swap."""
    system: str
    week: str
    opponent_player_id: int
    player_1_id: Optional[int] = None


class PrearrangedGameIn(SQLModel):
    """Request body for POST /signups/prearranged.

    Player B may be a real club member (player_b_id set) OR a guest / +1 who
    isn't on the system (player_b_id None + guest_b_name given). A guest has no
    Player row: their Signup is created with player_id NULL, so they never
    appear in the roster, leaderboard, or the auto-pairing pool.
    """
    system: str
    week: str
    player_a_id: int
    player_b_id: Optional[int] = None
    guest_b_name: Optional[str] = None
    faction_a: Optional[str] = None
    faction_b: Optional[str] = None
    eta: Optional[str] = None
    vibe: str = "Casual"
    points: Optional[int] = None


def _get_system_config(db: Session, legacy_system_name: str) -> Optional[SystemConfig]:
    """Look up the catalogue row by the exact string still stored in
    Signup.system / Pairing.system / PublishState.system today (see
    SystemConfig.legacy_system_name docstring in models.py)."""
    return db.exec(
        select(SystemConfig)
        .where(SystemConfig.legacy_system_name == legacy_system_name)
        .where(SystemConfig.active == True)
    ).first()


def _effective_vibe_config(db: Session, club_id: int, config: SystemConfig) -> tuple:
    """The vibe options + default a signup should be validated against for
    this club/system: the club's own ClubSystem.vibe_options override if set,
    otherwise the platform catalogue default (config.vibe_options). Returns
    (vibe_options, default_vibe). NULL/empty override → catalogue default, so
    a club that hasn't customized its vibes behaves exactly as before."""
    cs = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == club_id,
            ClubSystem.system_id == config.id,
        )
    ).first()
    if cs is not None and cs.vibe_options:
        options, default = cs.vibe_options, (cs.default_vibe or (cs.vibe_options[0] if cs.vibe_options else None))
    else:
        options, default = config.vibe_options, config.default_vibe
    # Only canonical vibes — drops any stale/removed value (e.g. the retired
    # "Escalation") that may still linger in catalogue data.
    options = [v for v in (options or []) if v in CANONICAL_VIBES]
    if default not in options:
        default = options[0] if options else None
    return options, default


def _require_system_enabled(db: Session, club_id: int, system: str) -> None:
    """422 unless the caller's club has this system enabled — checked on
    every signup-*creation* call site. Dropping an already-existing signup
    is exempt (see drop_signup) so a player who signed up before a system
    was disabled is never trapped."""
    row = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(SystemConfig.legacy_system_name == system)
        .where(ClubSystem.club_id == club_id)
    ).first()
    if row is None or not row[0].enabled:
        raise HTTPException(
            status_code=422, detail=f"{system} is not currently enabled for your club."
        )


def _require_linked_player(user: User, db: Session, club_id: int) -> Player:
    """The caller's player AT THE ACTIVE CLUB (multi-club network model). A user
    with no player at this club is told to claim/create one here — even if they
    have a player at another club."""
    pid = active_player_id_for(db, user, club_id)
    if pid is None:
        raise HTTPException(status_code=400, detail="No linked player profile at this club — claim your profile first.")
    player = db.get(Player, pid)
    if player is None or not player.active:
        raise HTTPException(status_code=400, detail="Linked player profile not found.")
    return player


def is_first_signup(db: Session, player_id: Optional[int], club_id: int, system: Optional[str] = None) -> bool:
    """Has this player never signed up before — at this club, or for this
    system if one is given?

    Shared detection point. Three separate features want this same question:
    the Discord gate below (only new faces are checked), new-player visibility
    for admins, and a "new challenger" flourish on a first signup post. Keep
    them on one implementation so they can never disagree about who counts as
    new. Guests (player_id None) are never "first" — they have no history to
    have, and nothing downstream should treat them as a new member.
    """
    if player_id is None:
        return False
    q = scoped(Signup, club_id).where(Signup.player_id == player_id)
    if system is not None:
        q = q.where(Signup.system == system)
    return db.exec(q).first() is None


def _discord_gate_mode(db: Session, club_id: int) -> str:
    """'off' | 'monitor' | 'enforce' for this club as a whole. Defaults to
    'off' so the gate is inert everywhere until a club deliberately turns it
    on. This is the FALLBACK for systems that set no mode of their own —
    resolve_discord_gate below is what call sites should go through."""
    mode = (get_setting(db, club_id, "discord_gate_mode", "off") or "off").strip().lower()
    return mode if mode in {"off", "monitor", "enforce"} else "off"


def _club_system_for(db: Session, club_id: int, system: Optional[str]) -> Optional[ClubSystem]:
    """This club's row for a system, addressed by the legacy display string
    that Signup.system and friends actually store. None when the caller
    didn't say which system, or the club doesn't run it."""
    if not system:
        return None
    config = _get_system_config(db, system)
    if config is None:
        return None
    return db.exec(
        scoped(ClubSystem, club_id).where(ClubSystem.system_id == config.id)
    ).first()


def resolve_discord_gate(
    db: Session, club_id: int, system: Optional[str]
) -> tuple[Optional[str], str, Optional[str], Optional[str]]:
    """Which Discord server this (club, system) is gated on, in what mode,
    with which invite link — returns (guild_id, mode, invite_url, server_label).

    Two things are going on here, and they resolve differently on purpose.

    WHETHER the gate applies is opt-in per system, with no inheritance: a game
    night is gated only if its own admin ticked discord_gate_enabled. A club
    running three systems can gate one and leave the other two alone, and no
    club-wide switch can quietly turn it on for a system that didn't ask for
    it. A system that hasn't opted in returns mode 'off' and never reaches
    Discord.

    WHICH SERVER it points at DOES fall back to the club, because that's a
    default rather than a policy: most clubs have one Discord for everything
    and shouldn't have to paste the same invite into every system, while
    EGNWGC — where Kill Team and The Old World are separate servers, neither
    of them club-wide — sets each system's own and the fallback never fires.

    server_label is what the player gets told to join. It names the SYSTEM
    when that system has its own server ("the Kill Team Discord") and the CLUB
    when it's inheriting — telling a player to "join EGNWGC's Discord" when
    there are three of them and only one will let them in is the failure this
    label exists to avoid.
    """
    club = db.get(Club, club_id)
    if club is None:
        return None, "off", None, None

    cs = _club_system_for(db, club_id, system)

    if system is not None:
        # System-scoped action: opt-in is the whole story.
        if cs is None or not cs.discord_gate_enabled:
            return None, "off", None, None
        # NULL mode means "opted in, still watching" — escalating to enforce
        # is always a deliberate second action by the admin.
        mode = (cs.discord_gate_mode or "monitor").strip().lower()
        if mode not in {"off", "monitor", "enforce"}:
            mode = "monitor"
    else:
        # No system in play (club-level action). Falls back to the original
        # club-wide gate, which is off at every club today. Kept so a caller
        # that can't name a system degrades to the old behaviour rather than
        # silently skipping the check.
        mode = _discord_gate_mode(db, club_id)

    guild_id = (cs.discord_guild_id if cs else None) or club.discord_guild_id
    invite_url = (cs.discord_url if cs else None) or club.discord_url

    if cs is not None and cs.discord_guild_id:
        label = f"the {system} Discord"
    else:
        label = f"{club.name}'s Discord"

    return guild_id, mode, invite_url, label


def require_discord_member(
    db: Session, player: Player, club_id: int, system: Optional[str] = None
) -> None:
    """Gate the commit-to-play actions on being in the right Discord server.

    The point isn't identity-checking for its own sake: pairings, drops and
    call-outs are all announced in that Discord, so someone outside it can't
    find out they've been paired. Being in the server is what makes the rest
    of the app work for them.

    `system` is the legacy display string ("Kill Team"), and picks WHICH
    server — a club can run each game night out of a different one. Omitting
    it falls back to the club-wide server, which is correct for the club-level
    actions that aren't tied to a system.

    Runs at most ONE Discord API call per player PER SERVER, ever — a verified
    player short-circuits on an indexed row read, which is why this is safe to
    put on the hot signup path. Fails open at every step: no guild id, no bot
    token, an unclaimed/guest player, or an undetermined answer all let the
    caller through.
    """
    # Blanket grandfather stamp from the original club-wide rollout: every
    # player who already existed when the gate first shipped is exempt from
    # every server's check, forever. Deliberate — that stamp exists precisely
    # so established members are never surprised by a new requirement, and
    # splitting the gate per system doesn't change who was already here.
    if player.discord_verified_at is not None:
        return

    guild_id, mode, invite_url, server_label = resolve_discord_gate(db, club_id, system)

    if not guild_id:
        return  # no server configured for this system or its club: gate off
    if mode == "off":
        return

    if _is_verified_for_guild(db, player.id, guild_id):
        return  # already checked against THIS server — the common path

    # No Discord identity to check: unclaimed roster entries and admin-created
    # players. An admin vouching for someone must never be blocked by this.
    if player.user_id is None:
        return
    account = db.get(User, player.user_id)
    if account is None or not account.discord_id:
        return

    member = discord_guild.is_guild_member(guild_id, account.discord_id)

    if member is True:
        _record_verification(db, player.id, guild_id)
        return

    if member is None:
        return  # undetermined — fail open (already reported by discord_guild)

    if mode == "monitor":
        # Deliberately allowed. Monitor mode exists so a club can see who
        # WOULD be blocked before anyone actually is; logged rather than
        # alerted so a busy signup night doesn't spam the alerts channel.
        print(
            f"[discord_gate] MONITOR would block player={player.id} "
            f"({player.name!r}) club={club_id} system={system!r} guild={guild_id}"
        )
        return

    club = db.get(Club, club_id)
    raise HTTPException(
        status_code=403,
        detail={
            "code": "discord_membership_required",
            "message": (
                f"Join {server_label} to take part — that's where pairings "
                f"and reminders for {system or 'this club'} get posted."
            ),
            "club_name": club.name if club else None,
            "system": system,
            "server_label": server_label,
            "discord_url": invite_url,
        },
    )


def _is_verified_for_guild(db: Session, player_id: Optional[int], guild_id: str) -> bool:
    """Whether this player has already been confirmed in this server."""
    if player_id is None:
        return False
    return db.exec(
        select(PlayerDiscordVerification)
        .where(PlayerDiscordVerification.player_id == player_id)
        .where(PlayerDiscordVerification.guild_id == guild_id)
    ).first() is not None


def _record_verification(db: Session, player_id: Optional[int], guild_id: str) -> None:
    """Cache a confirmed membership so this player is never checked against
    this server again.

    Swallows write failures on purpose: the player HAS been confirmed as a
    member, so the only cost of a failed insert is one extra Discord call
    next time. Turning that into a 500 would block a legitimate signup over
    a cache miss, which contradicts the fail-open rule the whole gate is
    built on. The unique index on (player_id, guild_id) means a concurrent
    double-signup lands here rather than writing two rows.
    """
    if player_id is None:
        return
    try:
        db.add(PlayerDiscordVerification(player_id=player_id, guild_id=guild_id))
        db.commit()
    except Exception as e:
        db.rollback()
        capture(e, kind="discord_gate_cache_write", guild_id=guild_id)


def _validate_week(week: str) -> str:
    week = week.strip()
    try:
        datetime.strptime(week, "%d/%m/%Y")
    except ValueError:
        raise HTTPException(status_code=422, detail="Week must be in DD/MM/YYYY format.")
    return week


def _signup_count_phrase_for_system(system: str) -> str:
    if system == "The Horus Heresy":
        return "HH session signups"
    if system == "The Old World":
        return "TOW signups this week"
    if system == "Kill Team":
        return "KT signups this week"
    return f"{system} signups this week"


def signup_cap(db: Session, club_id: int, system: str) -> dict:
    """Per-(club, system) signup cap, stored in club_settings (no dedicated
    table — mirrors auto-pairings). The cap is expressed in tables; the
    player limit is tables × players-per-table, reusing the table-booking
    config's players_per_table (fallback 2) so "table size" has one home.
    max_players is None when the cap is disabled."""
    slug = system_setting_slug(system)
    enabled = (get_setting(db, club_id, f"signup_cap_{slug}_enabled", "false") or "false").lower() == "true"
    try:
        tables = int(get_setting(db, club_id, f"signup_cap_{slug}_tables", "0") or "0")
    except ValueError:
        tables = 0

    players_per_table = 2
    config = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == system)).first()
    if config is not None:
        tb = db.exec(
            select(TableBookingConfig).where(
                TableBookingConfig.club_id == club_id,
                TableBookingConfig.system_id == config.id,
            )
        ).first()
        if tb is not None and tb.players_per_table:
            players_per_table = tb.players_per_table

    max_players = tables * players_per_table if (enabled and tables > 0) else None
    return {
        "enabled": enabled,
        "tables": tables,
        "players_per_table": players_per_table,
        "max_players": max_players,
    }


def _signup_count(db: Session, system: str, week: str, club_id: int) -> int:
    """Distinct players signed up for this week/system (latest row per player wins)."""
    rows = db.exec(
        scoped(Signup, club_id)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at.desc())
    ).all()
    seen = set()
    for s in rows:
        seen.add(s.player_id if s.player_id is not None else id(s))
    return len(seen)


# Discord webhooks are occasionally slow. 5s was too tight and produced
# ReadTimeouts on posts that had almost certainly landed; the weekly
# call-to-arms post already used 10s and image posts 30s. Connect stays short
# so a genuinely unreachable host still fails fast rather than stalling a
# signup request for 10 seconds.
_WEBHOOK_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _post_webhook(db: Session, club_id: int, system: str, content: str) -> None:
    """Fire-and-forget Discord post. Never breaks the request on failure."""
    system_config = _get_system_config(db, system)
    system_id = system_config.id if system_config else None
    url = resolve_webhook_url(db, club_id, "signup", system_id)
    if not url:
        return
    try:
        resp = httpx.post(
            url,
            # allowed_mentions restricts pings to explicit user mentions we
            # build ourselves. Without it Discord parses the whole message,
            # so a player whose name contained "@everyone" would ping the
            # server every time they signed up.
            json={"content": content, "allowed_mentions": {"parse": ["users"]}},
            timeout=_WEBHOOK_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.ReadTimeout:
        # The request WAS sent and Discord almost certainly processed it — we
        # just gave up waiting for the response. Deliberately not alerted:
        # it's ambiguous rather than broken, there's nothing to act on, and
        # retrying would risk double-posting. Logged so it's still traceable
        # if posts ever do go missing.
        print(
            f"[webhook] read timeout after send (probably delivered) "
            f"club={club_id} system={system!r}"
        )
    except Exception as e:
        # Everything else IS actionable — a revoked/deleted webhook (404), a
        # bad URL (401), or an unreachable host. Surface those.
        capture(e, kind="discord_webhook", club_id=club_id, system=system)


def _post_discord_signup(db: Session, player_name: str, faction: Optional[str], vibe: Optional[str], system: str, week: str, club_id: int, player_id: Optional[int] = None, first_ever: bool = False) -> None:
    """Announce a signup. A player's FIRST ever signup for this system gets a
    louder post — the club sees a new face arriving rather than another line
    in the weekly list, which is the moment someone is most likely to get a
    welcome. `first_ever` must be decided by the caller before the row is
    written (see submit_signup)."""
    faction_label = faction or "Unknown faction"
    vibe_label = vibe or "Unknown vibe"
    count = _signup_count(db, system, week, club_id)
    phrase = _signup_count_phrase_for_system(system)
    who = name_with_mention(db, player_name, player_id)

    if first_ever:
        content = (
            f"🎉 **A NEW CHALLENGER APPROACHES!**\n"
            f"{who} has joined the muster for their first game of {system} — "
            f"⚔️ {faction_label} • 🎭 {vibe_label}\n"
            f"📊 {phrase}: {count}\n"
            f"👋 Give them a warm welcome!"
        )
    else:
        content = f"📝 {who} signed up — ⚔️ {faction_label} • 🎭 {vibe_label}\n📊 {phrase}: {count}"

    _post_webhook(db, club_id, system, content)


def _post_discord_drop(db: Session, player_name: str, faction: Optional[str], vibe: Optional[str], system: str, week: str, club_id: int, player_id: Optional[int] = None) -> None:
    faction_label = faction or "Unknown faction"
    vibe_label = vibe or "Unknown vibe"
    count = _signup_count(db, system, week, club_id)
    phrase = _signup_count_phrase_for_system(system)
    who = name_with_mention(db, player_name, player_id)
    _post_webhook(db, club_id, system, f"❌ {who} dropped — ⚔️ {faction_label} • 🎭 {vibe_label}\n📊 {phrase}: {count}")


def _get_all_byes(db: Session, system: str, week: str, club_id: int) -> list[dict]:
    """Return all current BYE players for this week/system, ordered by player name."""
    pub = db.exec(
        scoped(PublishState, club_id)
        .where(PublishState.system == system)
        .where(PublishState.week == week)
    ).first()
    if not pub or not pub.published:
        return []
    bye_pairings = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.system == system)
        .where(Pairing.week == week)
        .where(Pairing.b_signup_id.is_(None))
    ).all()
    if not bye_pairings:
        return []
    signup_ids = [p.a_signup_id for p in bye_pairings]
    signups = db.exec(scoped(Signup, club_id).where(Signup.id.in_(signup_ids))).all()
    signups_by_id = {s.id: s for s in signups}
    result = []
    for p in bye_pairings:
        su = signups_by_id.get(p.a_signup_id)
        if su:
            result.append({
                "player_name": su.player_name,
                "player_id": su.player_id,
                "signup_id": p.a_signup_id,
                "is_new": False,
            })
    result.sort(key=lambda x: x["player_name"])
    return result


def _build_bye_discord_message(
    db: Session,
    header: str,
    newly_displaced_names: list[str],
    all_byes: list[dict],
    app_url: str,
) -> str:
    """Build a consistent Discord message for swap/drop events.

    Players left without an opponent are tagged, in the same
    `**Name** (<@id>)` format as the signup posts — this is the one message
    that specifically needs to reach the affected player, not just the
    channel.
    """
    if not all_byes:
        return f"{header}\n\n➡️ {app_url}" if app_url else header
    lines = [header, "", "⚠️ The following players are now without an opponent this week:"]
    for bye in all_byes:
        suffix = " (existing bye)" if not bye["is_new"] else ""
        who = name_with_mention(db, bye["player_name"], bye.get("player_id"))
        lines.append(f"• {who}{suffix}")
    lines.append("")
    lines.append(f"Head to the app to re-arrange your game: {app_url}")
    return "\n".join(lines)


class ExperienceAdjustmentIn(SQLModel):
    system: str
    extra_games: int


@router.get("/experience")
def my_experience(
    system: str,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """This player's standing in one system: games the club has paired them
    for, games they've added themselves, the total, and the tier it lands in.

    Read by the signup form to show the badge instead of asking. Returns the
    New tier with zero games for a caller with no player at this club, rather
    than 404 — the form wants something to render, not an error.
    """
    player_id = active_player_id_for(db, user, club_id)
    return experience_summary(db, club_id, player_id, system)


@router.post("/experience")
def set_my_experience_adjustment(
    body: ExperienceAdjustmentIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Record games this player has played elsewhere in this system.

    Added to the club's count, never replacing it — so it can only ever move
    someone UP a tier, and the tracked count keeps rising underneath. A player
    may only set their own; there is no player_id in the body for that reason,
    same rule as every other self-service write here.
    """
    player_id = active_player_id_for(db, user, club_id)
    if player_id is None:
        raise HTTPException(
            status_code=400,
            detail="No linked player profile at this club — claim your profile first.",
        )

    extra = body.extra_games
    if extra < 0:
        raise HTTPException(status_code=422, detail="Games played elsewhere can't be negative.")
    # A ceiling so a typo can't produce a nonsense profile. Well above any real
    # club career, and the tiers top out at 20 anyway.
    if extra > 1000:
        raise HTTPException(status_code=422, detail="That's more games than we can credit — 1000 is the maximum.")

    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    row = db.exec(
        select(PlayerExperienceAdjustment)
        .where(PlayerExperienceAdjustment.club_id == club_id)
        .where(PlayerExperienceAdjustment.player_id == player_id)
        .where(PlayerExperienceAdjustment.system == body.system)
    ).first()
    if row is None:
        row = PlayerExperienceAdjustment(
            club_id=club_id, player_id=player_id, system=body.system, extra_games=extra
        )
    else:
        row.extra_games = extra
        row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()

    return experience_summary(db, club_id, player_id, body.system)


@router.get("/level")
def my_level(
    system: str,
    player_id: Optional[int] = None,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Level and progress for a player in one system.

    Defaults to the caller; `player_id` reads someone else's, since levels are
    public — they show on profiles and get announced in Discord, so there is
    nothing to protect here.
    """
    target = player_id if player_id is not None else active_player_id_for(db, user, club_id)
    return level_progress(db, club_id, target, system)


@router.get("/mine")
def my_signup(
    system: str,
    week: str,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Return the user's signup for this exact week ('current') and their most
    recent signup for this system across any week ('last', used for prefill)."""
    player = _require_linked_player(user, db, club_id)
    week = _validate_week(week)

    current = db.exec(
        scoped(Signup, club_id)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .where(Signup.player_id == player.id)
        .order_by(Signup.id.desc())
    ).first()

    last = db.exec(
        scoped(Signup, club_id)
        .where(Signup.system == system)
        .where(Signup.player_id == player.id)
        .order_by(Signup.id.desc())
    ).first()

    return {"current": current, "last": last}


@router.post("")
def submit_signup(
    body: SignupIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    player = _require_linked_player(user, db, club_id)
    require_discord_member(db, player, club_id, body.system)

    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    _require_system_enabled(db, club_id, body.system)

    week = _validate_week(body.week)

    # Normalise exactly like the original form does
    faction = body.faction
    if faction in (None, "", "— None —"):
        faction = None

    # DERIVED, not taken from the request. Experience is now the club's own
    # count of games this player has been paired for in this system, plus any
    # games they've told us they played elsewhere — so it can't be re-answered
    # differently each week, and it moves people out of "New" on its own as
    # they play. The value is still WRITTEN to the signup row so everything
    # downstream (the matcher, the pairing cards, the posted image) keeps
    # reading one field, and so each signup keeps a record of where the player
    # stood at the time.
    experience = experience_summary(db, club_id, player.id, body.system)["tier"]

    # Catalogue-driven config. See SystemConfig in models.py for field meanings.
    eff_vibe_options, eff_default_vibe = _effective_vibe_config(db, club_id, config)
    vibe = body.vibe if body.vibe in (eff_vibe_options or []) else eff_default_vibe
    if config.uses_points:
        points = max(0, min(int(body.points or config.default_points), config.max_points))
    else:
        points = 0
    if config.uses_scenarios:
        scenario = (
            body.scenario if body.scenario in (config.scenario_options or [])
            else config.default_scenario
        )
    else:
        scenario = None
    can_demo = bool(body.can_demo) if config.allows_demo else False

    eta = (body.eta or "").strip() or None

    # Upsert: update the newest existing row, delete older duplicates
    existing = db.exec(
        scoped(Signup, club_id)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == player.id)
        .order_by(Signup.id.desc())
    ).all()

    created = not bool(existing)

    # Must be answered BEFORE the row is inserted below — once it exists, the
    # player trivially has a signup for this system and would never read as
    # new. Captured here and carried through to the Discord post.
    first_ever = created and is_first_signup(db, player.id, club_id, body.system)

    # Signup cap: block a *new* signup once the session is full. Players
    # editing their existing entry are always allowed through.
    if created:
        cap = signup_cap(db, club_id, body.system)
        if cap["max_players"] is not None:
            current = _signup_count(db, body.system, week, club_id)
            if current >= cap["max_players"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"This session is full ({cap['tables']} table{'s' if cap['tables'] != 1 else ''} · {cap['max_players']} players).",
                )
    if existing:
        su = existing[0]
        for dup in existing[1:]:
            db.delete(dup)
        su.player_name = player.name
        su.faction = faction
        su.points = points
        su.eta = eta
        su.experience = experience
        su.vibe = vibe
        su.standby_ok = bool(body.standby_ok) if config.uses_standby else False
        su.tnt_ok = False
        su.scenario = scenario
        su.can_demo = can_demo
        db.add(su)
    else:
        su = Signup(
            week=week, system=body.system,
            player_id=player.id, player_name=player.name,
            faction=faction, points=points, eta=eta,
            experience=experience, vibe=vibe,
            standby_ok=bool(body.standby_ok) if config.uses_standby else False, tnt_ok=False,
            scenario=scenario, can_demo=can_demo,
            club_id=club_id,
        )
        db.add(su)

    db.commit()
    db.refresh(su)

    if created:
        _post_discord_signup(db, player.name, faction, vibe, body.system, week, club_id, player_id=player.id, first_ever=first_ever)

    return {"ok": True, "created": created, "signup": su}


@router.delete("/mine")
def drop_signup(
    system: str,
    week: str,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    player = _require_linked_player(user, db, club_id)
    week = _validate_week(week)

    gate = db.exec(
        scoped(PublishState, club_id)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()

    if gate and gate.published:
        # Post-publish drop: reroute opponent to a BYE pairing, delete our pairing + signup
        rows = db.exec(
            scoped(Signup, club_id)
            .where(Signup.week == week)
            .where(Signup.system == system)
            .where(Signup.player_id == player.id)
        ).all()
        if not rows:
            return {"ok": True, "dropped": False}

        my_ids = {s.id for s in rows}

        pairing = db.exec(
            scoped(Pairing, club_id)
            .where(Pairing.week == week)
            .where(Pairing.system == system)
            .where((Pairing.a_signup_id.in_(my_ids)) | (Pairing.b_signup_id.in_(my_ids)))
        ).first()

        opponent_name: Optional[str] = None
        if pairing:
            if pairing.b_signup_id is not None:
                opponent_signup_id = (
                    pairing.b_signup_id if pairing.a_signup_id in my_ids
                    else pairing.a_signup_id
                )
                opponent_signup = db.get(Signup, opponent_signup_id)
                if opponent_signup:
                    opponent_name = opponent_signup.player_name
                    db.add(Pairing(
                        week=week, system=system,
                        a_signup_id=opponent_signup_id, b_signup_id=None,
                        status="pending", prearranged=False,
                        a_faction=opponent_signup.faction, b_faction=None,
                        club_id=club_id,
                    ))
            db.delete(pairing)

        for s in rows:
            db.delete(s)

        db.commit()

        all_byes = _get_all_byes(db, system, week, club_id)
        newly_displaced_names = [opponent_name] if opponent_name else []
        for bye in all_byes:
            if bye["player_name"] in newly_displaced_names:
                bye["is_new"] = True
        content = _build_bye_discord_message(
            db,
            header=f"❌ {name_with_mention(db, player.name, player.id)} has dropped out of this week's session.",
            newly_displaced_names=newly_displaced_names,
            all_byes=all_byes,
            app_url=APP_PUBLIC_URL,
        )
        _post_webhook(db, club_id, system, content)

        return {"ok": True, "dropped": True, "published": True}

    # Pre-publish drop path (unchanged)
    rows = db.exec(
        scoped(Signup, club_id)
        .where(Signup.week == week)
        .where(Signup.system == system)
        .where(Signup.player_id == player.id)
    ).all()
    if not rows:
        return {"ok": True, "dropped": False}

    ref = rows[0]
    ref_faction, ref_vibe = ref.faction, ref.vibe
    my_ids = {s.id for s in rows}

    # Delete any prearranged pairing involving the dropper; opponent's signup stays
    prearranged = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .where(Pairing.prearranged == True)
        .where((Pairing.a_signup_id.in_(my_ids)) | (Pairing.b_signup_id.in_(my_ids)))
    ).all()
    for p in prearranged:
        db.delete(p)

    for s in rows:
        db.delete(s)

    db.commit()

    _post_discord_drop(db, player.name, ref_faction, ref_vibe, system, week, club_id, player_id=player.id)

    return {"ok": True, "dropped": True}


@router.post("/prearranged")
def submit_prearranged(
    body: PrearrangedGameIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    # 1. System and week
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    _require_system_enabled(db, club_id, body.system)

    # Ownership: you may only arrange a game you are playing in. Without this,
    # any logged-in club member could book two arbitrary players against each
    # other — and because a pre-arranged game also blocks both of them out of
    # that week's pairings, it's a way to quietly disrupt someone else's club
    # night. The opponent is unrestricted: pick anyone on the club roster, or a
    # guest, exactly as before.
    #
    # System admins are exempt so they can still arrange a game on two members'
    # behalf. Same call as league.submit_result's ownership check, and for the
    # same reason: an admin can already generate, edit and delete pairings
    # outright, so refusing them here would protect nothing.
    #
    # A caller with no player at this club is NOT a participant by definition,
    # so they now need the admin scope. That's the tightening — this endpoint
    # used to let them through untouched.
    #
    # Checked BEFORE the Discord gate below: it's a local comparison, so a
    # caller who fails both shouldn't be sent to join a Discord server only to
    # be refused again on the second attempt for the real reason.
    caller_player_id = active_player_id_for(db, user, club_id)
    # player_b_id is None for a guest / +1, which simply means the caller has
    # to be player A — the comparison handles that without a special case.
    if caller_player_id is None or caller_player_id not in (body.player_a_id, body.player_b_id):
        if body.system not in admin_scopes(user, db, club_id):
            raise HTTPException(
                status_code=403,
                detail="You can only arrange games you're playing in.",
            )

    # The gate applies to the CALLER, who by the check above is either one of
    # the two players or this system's admin. An admin with no player row at
    # this club has no Discord identity to check, so they pass through.
    if caller_player_id is not None:
        caller_player = db.get(Player, caller_player_id)
        if caller_player is not None:
            require_discord_member(db, caller_player, club_id, body.system)

    week = _validate_week(body.week)

    # 2. Player A must be a real, active member of the caller's club.
    pa = db.get(Player, body.player_a_id)
    if pa is None or not pa.active or pa.club_id != club_id:
        raise HTTPException(status_code=404, detail="Player A not found.")

    # 3. Player B is either a real club member, or a guest / +1 (no profile).
    #    A guest has no Player row: player_b_id is None and guest_b_name is set.
    is_guest_b = body.player_b_id is None
    if is_guest_b:
        guest_name = (body.guest_b_name or "").strip()
        if not guest_name:
            raise HTTPException(status_code=422, detail="Please enter the guest's name.")
        guest_name = guest_name[:80]
        pb = None
        pb_player_id = None
        pb_name = guest_name
    else:
        if body.player_a_id == body.player_b_id:
            raise HTTPException(status_code=422, detail="Player A and Player B must be different.")
        pb = db.get(Player, body.player_b_id)
        if pb is None or not pb.active or pb.club_id != club_id:
            raise HTTPException(status_code=404, detail="Player B not found.")
        pb_player_id = pb.id
        pb_name = pb.name

    # 4. Both factions must be set
    faction_a = body.faction_a if body.faction_a not in (None, "", "— None —") else None
    faction_b = body.faction_b if body.faction_b not in (None, "", "— None —") else None
    if faction_a is None or faction_b is None:
        raise HTTPException(status_code=422, detail="Please pick a faction for both players.")

    # 5. Conflict check: no real player may already be signed up this week/system.
    #    A guest has no player_id, so there's nothing to conflict on for that side.
    real_ids = [body.player_a_id] + ([] if is_guest_b else [body.player_b_id])
    conflicts = db.exec(
        scoped(Signup, club_id)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id.in_(real_ids))
    ).all()
    if conflicts:
        names = sorted({s.player_name for s in conflicts})
        raise HTTPException(
            status_code=409,
            detail=f"Already signed up: {', '.join(names)}. They must drop first before being part of a pre-arranged game.",
        )

    # Normalise per system (catalogue-driven). Note this endpoint's "no points"
    # sentinel is None, not 0 like submit_signup — a pre-existing difference
    # between the two endpoints (Kill Team isn't points-based either way),
    # preserved deliberately rather than normalized, per user decision.
    eff_vibe_options, eff_default_vibe = _effective_vibe_config(db, club_id, config)
    vibe = body.vibe if body.vibe in (eff_vibe_options or []) else eff_default_vibe
    if config.uses_points:
        points = max(0, min(int(body.points or config.default_points), config.max_points))
    else:
        points = None

    eta = (body.eta or "").strip() or None

    # Create both signups and pairing in one transaction
    su_a = Signup(
        week=week, system=body.system,
        player_id=pa.id, player_name=pa.name,
        faction=faction_a, points=points, eta=eta,
        experience="New", vibe=vibe,
        standby_ok=False, tnt_ok=False,
        scenario=None, can_demo=False,
        club_id=club_id,
    )
    su_b = Signup(
        week=week, system=body.system,
        player_id=pb_player_id, player_name=pb_name,
        faction=faction_b, points=points, eta=eta,
        experience="New", vibe=vibe,
        standby_ok=False, tnt_ok=False,
        scenario=None, can_demo=False,
        club_id=club_id,
    )
    db.add(su_a)
    db.add(su_b)
    db.flush()

    pairing = Pairing(
        week=week, system=body.system,
        a_signup_id=su_a.id, b_signup_id=su_b.id,
        status="pending",
        a_faction=faction_a, b_faction=faction_b,
        prearranged=True,
        club_id=club_id,
    )
    db.add(pairing)
    db.commit()
    db.refresh(su_a)
    db.refresh(su_b)
    db.refresh(pairing)

    try:
        count = _signup_count(db, body.system, week, club_id)
        phrase = _signup_count_phrase_for_system(body.system)
        detail_parts = [f"🎭 {vibe}"]
        if eta:
            detail_parts.append(f"⏰ {eta}")
        if points is not None:
            detail_parts.append(f"🛡️ {points} pts")
        detail_line = " • ".join(detail_parts)
        # Tag both players, same "**Name** (<@id>)" format as the signup posts.
        # A guest has no Player row, so name_with_mention falls back to the
        # plain name and only the "(guest)" marker is added.
        a_label = name_with_mention(db, pa.name, pa.id)
        b_label = name_with_mention(db, pb_name, pb_player_id)
        if is_guest_b:
            b_label = f"{b_label} (guest)"
        # One player per line: a mention already renders as a chip, so
        # "**Name** (@tag) (Faction)" on one line read as two competing
        # bracketed groups. Faction can legitimately be unset, in which case
        # the dash is dropped rather than trailing an empty one.
        a_line = f"{a_label} — {faction_a}" if faction_a else a_label
        b_line = f"{b_label} — {faction_b}" if faction_b else b_label
        content = (
            f"🤝 **Pre-Arranged Game**\n"
            f"⚔️ {a_line}\n"
            f"🆚 {b_line}\n"
            f"{detail_line}\n"
            f"📊 {phrase}: {count}"
        )
        _post_webhook(db, club_id, body.system, content)
    except Exception:
        pass

    return {"ok": True, "signup_a": su_a, "signup_b": su_b, "pairing": pairing}


@router.post("/swap")
def swap_signups(
    body: SwapIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    week = _validate_week(body.week)

    # 1. Pairings must be published
    gate = db.exec(
        scoped(PublishState, club_id)
        .where(PublishState.week == week)
        .where(PublishState.system == body.system)
    ).first()
    if not gate or not gate.published:
        raise HTTPException(status_code=422, detail="Pairings are not published for this week.")

    # 2. Resolve player X.  Admins may supply player_1_id to act on behalf of
    #    any signed-up player; regular players are always player X themselves.
    if body.player_1_id is not None:
        if body.system not in admin_scopes(user, db, club_id):
            raise HTTPException(status_code=403, detail=f"Admin access for '{body.system}' required.")
        x_player_id = body.player_1_id
    else:
        player = _require_linked_player(user, db, club_id)
        x_player_id = player.id

    # 3. Find X signup
    x_signup = db.exec(
        scoped(Signup, club_id)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == x_player_id)
        .order_by(Signup.id.desc())
    ).first()
    if x_signup is None:
        detail = "Player 1 is not signed up for this week." if body.player_1_id is not None else "You are not signed up for this week."
        raise HTTPException(status_code=422, detail=detail)

    # 4. Must be different players
    if body.opponent_player_id == x_player_id:
        raise HTTPException(status_code=422, detail="Cannot swap with yourself.")

    # 4. Find Y (target player) signup
    y_signup = db.exec(
        scoped(Signup, club_id)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == body.opponent_player_id)
        .order_by(Signup.id.desc())
    ).first()
    if y_signup is None:
        raise HTTPException(status_code=422, detail="That player is not signed up for this week.")

    # 5. Find X's current pairing; capture X's old opponent signup_id
    x_pairing = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == body.system)
        .where((Pairing.a_signup_id == x_signup.id) | (Pairing.b_signup_id == x_signup.id))
    ).first()

    z_signup_id: Optional[int] = None
    if x_pairing:
        z_signup_id = (
            x_pairing.b_signup_id if x_pairing.a_signup_id == x_signup.id
            else x_pairing.a_signup_id
        )

    # 6. Find Y's current pairing; capture Y's old opponent signup_id
    y_pairing = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == body.system)
        .where((Pairing.a_signup_id == y_signup.id) | (Pairing.b_signup_id == y_signup.id))
    ).first()

    w_signup_id: Optional[int] = None
    if y_pairing:
        w_signup_id = (
            y_pairing.b_signup_id if y_pairing.a_signup_id == y_signup.id
            else y_pairing.a_signup_id
        )

    # 7. Edge case: X and Y are already paired with each other
    if x_pairing and y_pairing and x_pairing.id == y_pairing.id:
        return {"ok": True, "already_paired": True}

    # 8. Capture displaced player data before deleting
    z_signup: Optional[Signup] = db.get(Signup, z_signup_id) if z_signup_id is not None else None
    w_signup: Optional[Signup] = db.get(Signup, w_signup_id) if w_signup_id is not None else None

    # 9. Delete X's and Y's current pairings
    if x_pairing:
        db.delete(x_pairing)
    if y_pairing:
        db.delete(y_pairing)

    # 10. Create new X vs Y prearranged pairing
    db.add(Pairing(
        week=week, system=body.system,
        a_signup_id=x_signup.id, b_signup_id=y_signup.id,
        status="pending", prearranged=True,
        a_faction=x_signup.faction, b_faction=y_signup.faction,
        club_id=club_id,
    ))

    # 11. Create BYE pairings for each displaced real player
    if z_signup is not None:
        db.add(Pairing(
            week=week, system=body.system,
            a_signup_id=z_signup_id, b_signup_id=None,
            status="pending", prearranged=False,
            a_faction=z_signup.faction, b_faction=None,
            club_id=club_id,
        ))
    if w_signup is not None:
        db.add(Pairing(
            week=week, system=body.system,
            a_signup_id=w_signup_id, b_signup_id=None,
            status="pending", prearranged=False,
            a_faction=w_signup.faction, b_faction=None,
            club_id=club_id,
        ))

    # 12. Commit
    db.commit()

    # 13. Discord
    x_name = x_signup.player_name
    y_name = y_signup.player_name
    displaced = []
    if z_signup is not None:
        displaced.append({"player_id": z_signup.player_id, "player_name": z_signup.player_name})
    if w_signup is not None:
        displaced.append({"player_id": w_signup.player_id, "player_name": w_signup.player_name})

    all_byes = _get_all_byes(db, body.system, week, club_id)
    z_name = z_signup.player_name if z_signup is not None else None
    w_name = w_signup.player_name if w_signup is not None else None
    newly_displaced_names = [name for name in [z_name, w_name] if name]
    for bye in all_byes:
        if bye["player_name"] in newly_displaced_names:
            bye["is_new"] = True
    content = _build_bye_discord_message(
        db,
        header=(
            f"🔀 {name_with_mention(db, x_name, x_signup.player_id)} and "
            f"{name_with_mention(db, y_name, y_signup.player_id)} have re-arranged their games!"
        ),
        newly_displaced_names=newly_displaced_names,
        all_byes=all_byes,
        app_url=APP_PUBLIC_URL,
    )
    _post_webhook(db, club_id, body.system, content)

    # 14. Return
    return {
        "ok": True,
        "new_pairing": {
            "x_name": x_name,
            "y_name": y_name,
            "x_faction": x_signup.faction,
            "y_faction": y_signup.faction,
        },
        "displaced": displaced,
    }


@router.get("/unpaired")
def get_unpaired(
    system: str,
    week: str,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    week = _validate_week(week)

    gate = db.exec(
        scoped(PublishState, club_id)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()
    if not gate or not gate.published:
        return []

    bye_pairings = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .where(Pairing.b_signup_id.is_(None))
    ).all()

    result = []
    for p in bye_pairings:
        signup = db.get(Signup, p.a_signup_id)
        if signup:
            result.append({
                "player_id": signup.player_id,
                "player_name": signup.player_name,
                "signup_id": signup.id,
            })
    return result