"""Discord OAuth2 + session cookie management.

Flow:
  1. /auth/discord/login    -- redirect user to Discord's authorize URL
  2. /auth/discord/callback -- Discord redirects back here with ?code=...
                               we exchange code for token, fetch the user's
                               Discord identity, upsert the users row, set a
                               session cookie, redirect to frontend
  3. /auth/me               -- frontend uses this to ask "who am I logged in as?"
  4. /auth/logout           -- clear the cookie

Sessions are signed, and revocable per account: the cookie value is
`{user_id}.{hmac}`, `{user_id}:{session_version}.{hmac}` or
`{user_id}:{session_version}:{issued_at}.{hmac}`. We trust it iff the
signature verifies with SESSION_SECRET AND the version still matches the
user's session_version. The older two shapes still verify, so neither
addition logged anybody out. Bumping the version (identity.
bump_session_version) ends every session that account has.

The 30 days ROLL FORWARD: /auth/me re-issues a cookie older than
SESSION_REFRESH_AFTER, so someone who keeps using the app stays signed in and
someone who stops is signed out 30 days later. Every page calls /auth/me, so
that is the natural place for it. Without this the window ran from the last
sign-in, and a daily user was signed out a month later for no reason.

PROVIDERS NOTE (account overhaul Slab 0, 2026-09-15): a provider's callback
only turns its code into an identity.ProviderProfile. Everything after that,
finding or deferring the account, the cookie, and where to send them, is
_finish_sign_in and is shared by every provider. See ACCOUNT_OVERHAUL.md.

COOKIE NOTE: cta_session, cta_oauth_state, cta_oauth_return_to,
cta_oauth_next and cta_pending_signup all use samesite="lax" + secure=True
(the three cta_oauth_* cookies lacked secure until 2026-09-15). Lax is enough because the API is served from
api.calltoarms.app, which is same-site with every club subdomain — the
session cookie rides along on the frontend's credentialed fetches. Moving
the API to a different registrable domain would silently log everyone out
and require samesite="none". Chrome/Firefox treat http://localhost as
trustworthy, so secure=True still works for local dev.

RETURN NOTE: /discord/login stores WHERE (origin) and WHAT PAGE (next) in
two separate cookies. They were one concatenated value until 09/09/2026,
which made the brand-new-user branch below build
".../signup?system=X/join" — a path appended to a query string. Keep them
apart: the two branches join them differently.

SUBDOMAIN NOTE: login can be initiated from any club subdomain
(e.g. test1.calltoarms.app, manchester.calltoarms.app), not just the root
domain. FRONTEND_URL is a single fixed env var, so on its own it would
always bounce people back to the root domain after Discord auth regardless
of which subdomain they started on. _safe_return_to() captures the
initiating subdomain from the Referer header on /discord/login and carries
it through a short-lived cookie so /discord/callback can send the user back
to the right place. The regex restricts this to calltoarms.app (sub)domains
only, so a spoofed Referer can't be used as an open redirect.
"""
import base64
import hmac
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from database import (
    active_player_id_for, get_session, log_audit, resolve_active_club_id,
    resolve_request_club_id, scoped,
)
from identity import (
    DISCORD, EMAIL, GOOGLE, PASSWORD, ProviderProfile, account_with_verified_email, attach_identity,
    bump_session_version, clean_display_name, find_identity,
    create_user_for_profile,
    display_name_for, find_user_for_profile, identity_for, record_sign_in,
)
from models import (
    AdminRole, Club, ClubSystem, LoginAttempt, PasswordCredential, Player, SystemConfig,
    User, UserIdentity,
)
import email_login
import passwords
import user_merge
from emailer import UndeliverableRecipient
from observability import capture

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# The one frontend route a signed-in-but-account-less person is allowed to
# reach directly. Kept as a constant because two places have to agree on it:
# the callback below, and the frontend route that reads the pending cookie.
CLUB_REQUEST_PATH = "/request-club"
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

DISCORD_API = "https://discord.com/api"
SCOPES = "identify"

# Google (account overhaul Slab 4). Nothing about Google is visible anywhere
# until GOOGLE_CLIENT_ID is set.
#
# GOOGLE_SIGNIN decides how far it reaches:
#   "link"  Google can only be ADDED to an account someone is already signed in
#           to, from /account. The default, and where it starts: until regulars
#           have linked, a "Sign in with Google" button would hand every Discord
#           player who pressed it a second, empty account (no Discord emails are
#           held to match them on, Decision D).
#   "open"  Also a sign-in button on every sign-in screen.
# Switching is a Fly secret, not a deploy.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_SIGNIN = os.environ.get("GOOGLE_SIGNIN", "link")
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Email links (Slab 5). Same stages as Google, plus "off", which is the default:
# the Resend secrets this needs are already set for other mail, so presence of
# secrets can't be what switches it on.
EMAIL_SIGNIN = os.environ.get("EMAIL_SIGNIN", "off")
# Email and password (Slab 8). Same three stages. Needs email working, since
# signing up confirms the address and a forgotten password is emailed.
PASSWORD_SIGNIN = os.environ.get("PASSWORD_SIGNIN", "off")

# Failed password attempts allowed before a wait, per address and per IP.
PASSWORD_FAIL_LIMIT_EMAIL = 10
PASSWORD_FAIL_LIMIT_IP = 30
PASSWORD_FAIL_WINDOW = timedelta(minutes=15)


def _email_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("EMAIL_FROM"))


def linkable_providers() -> list[str]:
    """Sign-in methods a signed-in account can add from /account."""
    providers = []
    if DISCORD_CLIENT_ID:
        providers.append(DISCORD)
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        providers.append(GOOGLE)
    if EMAIL_SIGNIN in ("link", "open") and _email_configured():
        providers.append(EMAIL)
    if PASSWORD_SIGNIN in ("link", "open") and _email_configured():
        providers.append(PASSWORD)
    return providers


def sign_in_providers() -> list[str]:
    """Sign-in methods offered on the sign-in screens, in order."""
    providers = [DISCORD]
    linkable = linkable_providers()
    if GOOGLE in linkable and GOOGLE_SIGNIN == "open":
        providers.append(GOOGLE)
    if EMAIL in linkable and EMAIL_SIGNIN == "open":
        providers.append(EMAIL)
    if PASSWORD in linkable and PASSWORD_SIGNIN == "open":
        providers.append(PASSWORD)
    return providers

router = APIRouter(prefix="/auth", tags=["auth"])

# Matches calltoarms.app and any subdomain of it (www, test1, manchester, ...).
# Used to validate the Referer-derived return_to origin so /discord/login
# can't be abused as an open redirect to an arbitrary host.
_ALLOWED_RETURN_HOST_RE = re.compile(r"^([a-zA-Z0-9-]+\.)?calltoarms\.app$")


def _sign(value: str) -> str:
    """HMAC-sign a string with SESSION_SECRET so we can verify it later."""
    if not SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET is not set")
    return hmac.new(
        SESSION_SECRET.encode(),
        value.encode(),
        hashlib.sha256,
    ).hexdigest()


SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
# How old a cookie gets before /auth/me hands out a fresh one. Well under
# SESSION_MAX_AGE, so an active session is never close to expiring.
SESSION_REFRESH_AFTER = timedelta(days=7)


def _make_session_cookie(user_id: int, version: int = 0, issued_at: Optional[int] = None) -> str:
    """The signed session cookie value.

    Written as `user_id:version:issued_at`. The two older shapes (`user_id` and
    `user_id:version`) still verify on the way in, so nothing was logged out
    when each field arrived; `issued_at` is what lets /auth/me roll the 30 days
    forward. Passing neither version nor issued_at gives the original shape
    byte for byte, which the tests lean on.
    """
    if issued_at is None and not version:
        body = str(user_id)
    elif issued_at is None:
        body = f"{user_id}:{version}"
    else:
        body = f"{user_id}:{version}:{issued_at}"
    return f"{body}.{_sign(body)}"


def _parse_session_cookie(raw: Optional[str]) -> Optional[tuple[int, int, Optional[int]]]:
    """(user_id, version, issued_at) if the cookie is untampered, else None.
    issued_at is None for a cookie from before it was stamped. Says nothing
    about whether the version is still current; _session_user checks that."""
    if not raw or "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    parts = body.split(":")
    if len(parts) > 3:
        return None
    try:
        uid = int(parts[0])
        version = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
        issued = int(parts[2]) if len(parts) > 2 and parts[2] != "" else None
    except ValueError:
        return None
    return uid, version, issued


def _verify_session_cookie(raw: str) -> Optional[int]:
    """If the cookie is valid and untampered, return the user_id. Otherwise None.
    Signature only: use _session_user for anything that trusts the session."""
    parsed = _parse_session_cookie(raw)
    return parsed[0] if parsed else None


def _session_user(db: Session, raw: Optional[str]) -> Optional[User]:
    """The account a session cookie signs in, or None if it is missing,
    tampered with, or from before the account's sessions were ended."""
    parsed = _parse_session_cookie(raw)
    if parsed is None:
        return None
    user = db.get(User, parsed[0])
    if user is None or parsed[1] != (user.session_version or 0):
        return None
    return user


def _set_session_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        "cta_session",
        _make_session_cookie(user.id, user.session_version or 0,
                             int(datetime.utcnow().timestamp())),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=True,
    )


def _make_pending_profile_cookie(profile: ProviderProfile) -> str:
    """Same 'body.signature' shape as the session cookie, but the body is a
    base64-encoded JSON payload carrying the identity of someone signed in
    with a provider who has no account yet (see _finish_sign_in's new-person
    branch): enough to create the account in complete-signup without asking
    the provider again."""
    body = base64.urlsafe_b64encode(json.dumps(profile.to_payload()).encode()).decode()
    return f"{body}.{_sign(body)}"


def _make_pending_signup_cookie(discord_id: str, discord_name: str, avatar_url: Optional[str]) -> str:
    """A pending cookie for a Discord identity. Kept for the tests and for any
    caller that only has Discord's fields."""
    return _make_pending_profile_cookie(ProviderProfile(
        provider=DISCORD, subject=discord_id, name=discord_name, avatar_url=avatar_url,
    ))


def _verify_pending_signup_cookie(raw: Optional[str]) -> Optional[ProviderProfile]:
    """If the cookie is valid and untampered, return the identity it carries.
    Otherwise None (missing, signature mismatch, or malformed body). Accepts
    the Discord-only payload that predates providers."""
    if not raw or "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return ProviderProfile.from_payload(payload)


def _safe_return_to(request: Request) -> str:
    """Work out which frontend origin to send the user back to after login.

    Defaults to FRONTEND_URL (the root domain). If the login was initiated
    from a recognized calltoarms.app subdomain — inferred from the Referer
    header on the /discord/login navigation — return that origin instead,
    so club subdomains land back on themselves rather than bouncing to root.
    """
    referer = request.headers.get("referer")
    if not referer:
        return FRONTEND_URL
    parsed = urlparse(referer)
    if parsed.scheme != "https" or not parsed.hostname:
        return FRONTEND_URL
    if not _ALLOWED_RETURN_HOST_RE.match(parsed.hostname):
        return FRONTEND_URL
    return f"{parsed.scheme}://{parsed.netloc}"


def requester_identity(
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    cta_pending_signup: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
) -> Optional[dict]:
    """The sign-in identity behind a request, from EITHER a real session or a
    half-finished sign-in. None if neither is present.

    Two sources because of a bootstrap problem: a club organiser signing up to
    ask for their own club cannot have a User row, since users.club_id is NOT
    NULL and the club they are asking for is the one that does not exist yet.
    Their sign-in legitimately stops at the signed, short-lived
    cta_pending_signup cookie — the same one /join reads — and that is a real,
    verified identity even though no account exists behind it.

    Returns {provider, subject, name, discord_id, discord_name, user_id}.
    discord_id/discord_name are None unless the identity is Discord (they are
    what the reviewer sees on a club request). For a real session the identity
    is the account's Discord one if it has one, else its first identity.
    Callers that need an actual account should keep using require_user.
    """
    if session_cookie:
        user = _session_user(db, session_cookie)
        if user is not None:
            ident = identity_for(db, user.id, DISCORD)
            if ident is None:
                ident = db.exec(
                    select(UserIdentity).where(UserIdentity.user_id == user.id)
                    .order_by(UserIdentity.id)
                ).first()
            if ident is not None:
                provider, subject = ident.provider, ident.provider_user_id
            else:
                # Not yet backfilled: the mirror is all there is.
                provider, subject = DISCORD, user.discord_id
            return {
                "provider": provider,
                "subject": subject,
                "name": display_name_for(user),
                "discord_id": subject if provider == DISCORD else None,
                "discord_name": user.discord_name if provider == DISCORD else None,
                "user_id": user.id,
            }

    pending = _verify_pending_signup_cookie(cta_pending_signup)
    if pending is not None:
        is_discord = pending.provider == DISCORD
        return {
            "provider": pending.provider,
            "subject": pending.subject,
            "name": pending.name,
            "discord_id": pending.subject if is_discord else None,
            "discord_name": pending.name if is_discord else None,
            "user_id": None,
        }
    return None


def current_user(
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    db: Session = Depends(get_session),
) -> Optional[User]:
    """Resolve the current user from the session cookie, or None if not logged
    in (or that account's sessions have since been ended)."""
    return _session_user(db, session_cookie)


def require_user(user: Optional[User] = Depends(current_user)) -> User:
    """Like current_user, but raise 401 if not authenticated."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def active_club_id(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> int:
    """Dependency: the club this authenticated request is acting in
    (multi-club network model). Resolved from the subdomain the user is on,
    falling back to their soft home club. Use this instead of `user.club_id`
    for club-scoped reads/writes so a user can play at any club they visit —
    admin authorization stays separate (admin_roles for the resolved club)."""
    return resolve_active_club_id(db, user, request.headers.get("origin"))


def public_club_id(
    request: Request,
    club: Optional[str] = None,
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
) -> int:
    """Dependency: the club a request is scoped to, whether or not anyone is
    signed in. The optional-auth twin of active_club_id, for pages a stranger
    is meant to be able to read — the club's own landing page, and the
    booking form's availability reads.

    A signed-in caller lands in resolve_active_club_id exactly as
    active_club_id does, so a real browser request resolves to the same club
    as before and swapping an endpoint over adds an anonymous path where
    there used to be a 401. The one difference: an explicit `?club=` param is
    forwarded here and was ignored by active_club_id, so a signed-in caller
    can now aim these endpoints at another club. That is deliberate and safe
    only because every endpoint using this dependency is public by design —
    an anonymous caller could pass the same param and read the same data, so
    there is nothing to escalate to. Do NOT reach for this on an endpoint
    that returns anything member-only; that is what active_club_id is for.

    Anonymous callers resolve by subdomain (Origin), with `?club=` taking
    precedence for SSR loaders and tooling that can't carry a real browser
    Origin. See resolve_request_club_id for the full order.
    """
    try:
        return resolve_request_club_id(db, user, club, request.headers.get("origin"))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


def _safe_next_path(raw: str | None) -> str:
    """A caller-supplied path to land on after login, or "" if it isn't one.

    Only ever a path on the origin we already resolved — never a full URL. The
    checks below exist so this can't be turned into an open redirect: a value
    like "//evil.com" or "https://evil.com" is a perfectly good relative-looking
    string to a careless join, and would send someone who clicked a link in a
    club's Discord straight off the app.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return ""
    if "\\" in raw or "\n" in raw or "\r" in raw:
        return ""
    return raw[:300]


@router.get("/discord/login")
def discord_login(request: Request, next: Optional[str] = None):
    """Step 1: send the browser to Discord's authorize page.

    `next` carries the path to land on afterwards, so someone who followed a
    "sign up for Wednesday" link out of Discord comes back to that signup page
    rather than to the club's front door with no idea why they signed in.
    """
    if not DISCORD_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

    state = secrets.token_urlsafe(24)
    # Origin and destination are kept APART, not concatenated. They used to be
    # glued together here, which made the brand-new-user branch in the callback
    # build "https://club.calltoarms.app/signup?system=X/join" — a path stuck on
    # the end of a query string. Keeping them separate lets the callback join
    # them correctly for each of its two very different destinations.
    origin = _safe_return_to(request)
    next_path = _safe_next_path(next)
    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"
    response = _oauth_redirect(auth_url, state, origin, next_path)
    # A Google link abandoned half-way must not follow this sign-in around.
    response.delete_cookie("cta_oauth_link")
    return response


def _oauth_redirect(auth_url: str, state: str, origin: str, next_path: str) -> RedirectResponse:
    """Send the browser to a provider, remembering where to come back to."""
    response = RedirectResponse(auth_url)
    response.set_cookie("cta_oauth_state", state, max_age=300, httponly=True, samesite="lax", secure=True)
    response.set_cookie("cta_oauth_return_to", origin, max_age=300, httponly=True, samesite="lax", secure=True)
    response.set_cookie("cta_oauth_next", next_path, max_age=300, httponly=True, samesite="lax", secure=True)
    return response


def _google_authorize_url(state: str) -> str:
    return f"{GOOGLE_AUTHORIZE_URL}?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": f"{BACKEND_URL}/auth/google/callback",
        "state": state,
        # Someone signed in to a work and a personal Google account should get
        # to pick, rather than have whichever is active chosen for them.
        "prompt": "select_account",
    })


@router.get("/google/login")
def google_login(request: Request, next: Optional[str] = None):
    """Sign in with Google. Only while GOOGLE_SIGNIN is "open"; see its note."""
    if GOOGLE not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Google sign-in isn't available.")
    state = secrets.token_urlsafe(24)
    response = _oauth_redirect(_google_authorize_url(state), state, _safe_return_to(request), _safe_next_path(next))
    response.delete_cookie("cta_oauth_link")
    return response


def _link_cookie_value(user: User) -> str:
    body = f"link:{user.id}:{user.session_version or 0}"
    return f"{body}.{_sign(body)}"


def _link_user(db: Session, raw: Optional[str], session_cookie: Optional[str]) -> Optional[User]:
    """The account a link flow was started from, if the link cookie is genuine
    and the same account is still signed in on this browser."""
    if not raw or "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        _, uid, ver = body.split(":")
        uid, ver = int(uid), int(ver)
    except ValueError:
        return None
    user = _session_user(db, session_cookie)
    if user is None or user.id != uid or (user.session_version or 0) != ver:
        return None
    return user


@router.get("/google/link")
def google_link(
    request: Request,
    user: Optional[User] = Depends(current_user),
):
    """Add Google to the signed-in account (from /account). Comes back to
    /account with ?linked=google or ?link_error=... either way."""
    origin = _safe_return_to(request)
    if GOOGLE not in linkable_providers():
        return RedirectResponse(f"{origin}/account?link_error=unavailable")
    if user is None:
        return RedirectResponse(f"{origin}/account")
    state = secrets.token_urlsafe(24)
    response = _oauth_redirect(_google_authorize_url(state), state, origin, "/account")
    response.set_cookie("cta_oauth_link", _link_cookie_value(user), max_age=300,
                        httponly=True, samesite="lax", secure=True)
    return response


@router.get("/google/callback")
async def google_callback(
    state: str,
    code: Optional[str] = None,
    error: Optional[str] = None,
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    cta_oauth_state: Optional[str] = Cookie(default=None),
    cta_oauth_return_to: Optional[str] = Cookie(default=None),
    cta_oauth_next: Optional[str] = Cookie(default=None),
    cta_oauth_link: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """Google redirected back. Finishes either a link or a sign-in."""
    if not cta_oauth_state or cta_oauth_state != state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")
    origin = cta_oauth_return_to or FRONTEND_URL

    if cta_oauth_link:
        linker = _link_user(db, cta_oauth_link, session_cookie)
        if error or not code:
            return _link_done(origin, "link_error=cancelled")
        if linker is None:
            return _link_done(origin, "link_error=signed_out")
        profile = await _google_profile(code)
        return _finish_link(db, linker, profile, origin)

    if error or not code:
        # They backed out on Google's screen. Nothing to do but go back.
        response = RedirectResponse(origin + _safe_next_path(cta_oauth_next))
        _clear_oauth_cookies(response)
        return response
    if GOOGLE not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Google sign-in isn't available.")
    profile = await _google_profile(code)
    return _finish_sign_in(db, profile, cta_oauth_return_to, cta_oauth_next)


async def _google_profile(code: str) -> ProviderProfile:
    """Turn Google's authorisation code into who they are.

    The code is exchanged server to server over TLS, so the userinfo response
    can be trusted as it stands; there is no ID token to verify.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{BACKEND_URL}/auth/google/callback",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google sign-in failed. Please try again.")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="Google sign-in failed. Please try again.")
        info_resp = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if info_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Google sign-in failed. Please try again.")
        info = info_resp.json()

    if not info.get("sub"):
        raise HTTPException(status_code=400, detail="Google sign-in failed. Please try again.")
    return ProviderProfile(
        provider=GOOGLE,
        subject=str(info["sub"]),
        name=info.get("name") or info.get("given_name"),
        avatar_url=info.get("picture"),
        email=info.get("email"),
        email_verified=bool(info.get("email_verified")),
    )


def _link_done(origin: str, query: str) -> RedirectResponse:
    response = RedirectResponse(f"{origin}/account?{query}")
    _clear_oauth_cookies(response)
    response.delete_cookie("cta_oauth_link")
    return response


def _link_outcome(db: Session, user: User, profile: ProviderProfile) -> tuple[str, Optional[str]]:
    """Attach `profile` to the signed-in `user`. Returns (query string for
    /account, merge cookie value or None).

    If the identity already belongs to a DIFFERENT account, the person has now
    proven they hold both: they are signed in to this one, and they just
    completed the other's sign-in. So instead of refusing, they are offered a
    merge (Slab 6), which they confirm on /account after seeing what moves.
    """
    existing = find_identity(db, profile.provider, profile.subject)
    if existing is not None and existing.user_id != user.id:
        return f"merge=pending&provider={profile.provider}", _merge_cookie_value(user, existing.user_id)
    attach_identity(db, user, profile)
    if existing is None:
        log_audit(db, user, "identity.link", "user", user.id, f"{profile.provider} {profile.email or profile.subject}")
    db.commit()
    return f"linked={profile.provider}", None


def _finish_link(db: Session, user: User, profile: ProviderProfile, origin: str) -> RedirectResponse:
    query, merge_cookie = _link_outcome(db, user, profile)
    response = _link_done(origin, query)
    if merge_cookie:
        _set_merge_cookie(response, merge_cookie)
    return response


MERGE_OFFER_SECONDS = 600


def _merge_cookie_value(user: User, drop_id: int) -> str:
    """Signed (keep, drop, keep's session version, expiry). Only the account
    that proved both can confirm, and only for ten minutes."""
    expires = int(datetime.utcnow().timestamp()) + MERGE_OFFER_SECONDS
    body = f"merge:{user.id}:{drop_id}:{user.session_version or 0}:{expires}"
    return f"{body}.{_sign(body)}"


def _set_merge_cookie(response: Response, value: str) -> None:
    response.set_cookie("cta_merge", value, max_age=MERGE_OFFER_SECONDS,
                        httponly=True, samesite="lax", secure=True)


def _merge_offer(db: Session, raw: Optional[str], user: User) -> Optional[int]:
    """The account id `user` may merge in, from a genuine unexpired offer
    made to this same account and session, else None."""
    if not raw or "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        _, keep, drop, ver, expires = body.split(":")
        keep, drop, ver, expires = int(keep), int(drop), int(ver), int(expires)
    except ValueError:
        return None
    if keep != user.id or ver != (user.session_version or 0):
        return None
    if expires < int(datetime.utcnow().timestamp()):
        return None
    return drop if db.get(User, drop) is not None else None


def _request_origin(request: Request) -> str:
    """The calltoarms.app origin a fetch came from, for building links back to
    it. Origin header first (fetches carry it), then Referer, then the default
    frontend. Anything not on calltoarms.app falls back, as in _safe_return_to."""
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme == "https" and parsed.hostname and _ALLOWED_RETURN_HOST_RE.match(parsed.hostname):
            return f"{parsed.scheme}://{parsed.netloc}"
    return _safe_return_to(request)


class EmailStartBody(BaseModel):
    email: str
    next: Optional[str] = None


_EMAIL_SENT = {
    "ok": True,
    # The same words whether or not the address has an account, so this can't
    # be used to find out who plays here.
    "detail": "If that address can be used, a sign-in link is on its way. It works for 15 minutes.",
}


def _send_or_explain(email: str, purpose: str, url: str) -> None:
    try:
        email_login.send_link(email, purpose, url)
    except UndeliverableRecipient:
        # A mistyped address. Saying so would reveal nothing about accounts,
        # but it would differ from the success answer, so stay quiet.
        pass
    except RuntimeError as e:
        capture(e, where="email sign-in link")
        raise HTTPException(status_code=503, detail="We couldn't send email just now. Please try again shortly.")


@router.post("/email/start")
def email_start(body: EmailStartBody, request: Request, db: Session = Depends(get_session)):
    """Email a sign-in link. Only while EMAIL_SIGNIN is "open"."""
    if EMAIL not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Email sign-in isn't available.")
    email = email_login.normalise_email(body.email)
    if email is None:
        raise HTTPException(status_code=422, detail="That doesn't look like an email address.")
    origin = _request_origin(request)
    try:
        token = email_login.issue(db, email=email, purpose=email_login.SIGN_IN, origin=origin,
                                  ip=email_login.client_ip(request), next_path=_safe_next_path(body.next))
    except email_login.RateLimited:
        raise HTTPException(status_code=429, detail="That's a lot of sign-in links. Please wait a while and try again.")
    db.commit()
    _send_or_explain(email, email_login.SIGN_IN, email_login.link_url(origin, token))
    return _EMAIL_SENT


class EmailLinkBody(BaseModel):
    email: str


@router.post("/email/link")
def email_link(
    body: EmailLinkBody,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Email a link that adds this address to the signed-in account."""
    if EMAIL not in linkable_providers():
        raise HTTPException(status_code=404, detail="Adding an email isn't available.")
    email = email_login.normalise_email(body.email)
    if email is None:
        raise HTTPException(status_code=422, detail="That doesn't look like an email address.")
    origin = _request_origin(request)
    try:
        token = email_login.issue(db, email=email, purpose=email_login.LINK, origin=origin,
                                  ip=email_login.client_ip(request), user_id=user.id)
    except email_login.RateLimited:
        raise HTTPException(status_code=429, detail="That's a lot of links. Please wait a while and try again.")
    db.commit()
    _send_or_explain(email, email_login.LINK, email_login.link_url(origin, token))
    return {"ok": True, "detail": "Check that inbox for a link to confirm it. It works for 15 minutes."}


class EmailVerifyBody(BaseModel):
    token: str


@router.post("/email/verify")
def email_verify(
    body: EmailVerifyBody,
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    db: Session = Depends(get_session),
):
    """The Continue button on the page an emailed link opens. Spends the token
    and answers {"redirect": url}, setting whatever cookie a sign-in sets."""
    try:
        row = email_login.peek(db, body.token)
    except email_login.InvalidToken:
        raise HTTPException(status_code=410, detail="That link has expired or been used. Ask for a new one.")

    profile = ProviderProfile(provider=EMAIL, subject=row.email, email=row.email, email_verified=True)

    if row.purpose == email_login.LINK:
        # Checked BEFORE spending, so opening the link on the wrong device
        # doesn't waste it.
        user = _session_user(db, session_cookie)
        if user is None or user.id != row.user_id:
            raise HTTPException(
                status_code=403,
                detail="Open this link in the browser where you're signed in to the account you're adding it to.",
            )
        email_login.spend(db, row)
        db.flush()
        query, merge_cookie = _link_outcome(db, user, profile)
        db.commit()
        out = JSONResponse({"redirect": f"{row.origin}/account?{query}"})
        if merge_cookie:
            _set_merge_cookie(out, merge_cookie)
        return out

    if row.purpose == email_login.PASSWORD_SIGNUP:
        if PASSWORD not in sign_in_providers():
            raise HTTPException(status_code=404, detail="Signing up with a password isn't available.")
        # The address is confirmed, so this becomes a password identity; the
        # password itself is waiting on the token row for complete-signup.
        profile = ProviderProfile(provider=PASSWORD, subject=row.email, email=row.email, email_verified=True)
    elif EMAIL not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Email sign-in isn't available.")
    email_login.spend(db, row)
    db.flush()
    redirect = _finish_sign_in(db, profile, row.origin, row.next_path)
    out = JSONResponse({"redirect": redirect.headers["location"]})
    for header in redirect.headers.getlist("set-cookie"):
        out.headers.append("set-cookie", header)
    return out


@router.get("/discord/link")
def discord_link(request: Request, user: Optional[User] = Depends(current_user)):
    """Add a Discord account to the signed-in account (from /account),
    including a second one: the Discord account that's actually in a club's
    server, for someone who signs in with another (KNOWN_ISSUES.md #1)."""
    origin = _safe_return_to(request)
    if not DISCORD_CLIENT_ID:
        return RedirectResponse(f"{origin}/account?link_error=unavailable")
    if user is None:
        return RedirectResponse(f"{origin}/account")
    state = secrets.token_urlsafe(24)
    auth_url = f"{DISCORD_API}/oauth2/authorize?" + urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": f"{BACKEND_URL}/auth/discord/callback",
        "state": state,
        # Shows Discord's authorise screen rather than skipping it. It does NOT
        # let them pick a different Discord account: Discord uses whichever is
        # logged in to discord.com in this browser, so adding a second one means
        # logging in to that one there first (/account says so).
        "prompt": "consent",
    })
    response = _oauth_redirect(auth_url, state, origin, "/account")
    response.set_cookie("cta_oauth_link", _link_cookie_value(user), max_age=300,
                        httponly=True, samesite="lax", secure=True)
    return response


@router.get("/discord/callback")
async def discord_callback(
    state: str,
    code: Optional[str] = None,
    error: Optional[str] = None,
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    cta_oauth_state: Optional[str] = Cookie(default=None),
    cta_oauth_return_to: Optional[str] = Cookie(default=None),
    cta_oauth_next: Optional[str] = Cookie(default=None),
    cta_oauth_link: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """Step 2: Discord redirected back with a ?code= — exchange it for a user,
    or finish adding a Discord account to one."""
    if not cta_oauth_state or cta_oauth_state != state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")
    origin = cta_oauth_return_to or FRONTEND_URL

    if cta_oauth_link:
        linker = _link_user(db, cta_oauth_link, session_cookie)
        if error or not code:
            return _link_done(origin, "link_error=cancelled")
        if linker is None:
            return _link_done(origin, "link_error=signed_out")
        return _finish_link(db, linker, await _discord_profile(code), origin)

    if error or not code:
        response = RedirectResponse(origin + _safe_next_path(cta_oauth_next))
        _clear_oauth_cookies(response)
        return response
    profile = await _discord_profile(code)
    return _finish_sign_in(db, profile, cta_oauth_return_to, cta_oauth_next)


async def _discord_profile(code: str) -> ProviderProfile:
    """Turn Discord's authorisation code into who they are. The only part of
    signing in that is Discord's; everything after is _finish_sign_in."""
    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_resp = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Discord token exchange failed: {token_resp.text}")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access_token in Discord response")

        user_resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch Discord user")
        discord_user = user_resp.json()

    discord_id = discord_user["id"]
    avatar_hash = discord_user.get("avatar")
    return ProviderProfile(
        provider=DISCORD,
        subject=discord_id,
        name=discord_user.get("global_name") or discord_user.get("username", "Unknown"),
        avatar_url=(
            f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
            if avatar_hash else None
        ),
    )


def _finish_sign_in(
    db: Session,
    profile: ProviderProfile,
    return_to_cookie: Optional[str],
    next_cookie: Optional[str],
) -> RedirectResponse:
    """The provider-neutral half of every sign-in: find the account, or defer
    creating one, then send them where they were going."""
    existing = find_user_for_profile(db, profile)
    origin = return_to_cookie or FRONTEND_URL
    # Re-validated rather than trusted: the cookie is ours and HttpOnly, but a
    # path that reaches a redirect deserves the same check on the way out as it
    # got on the way in.
    next_path = _safe_next_path(next_cookie)

    if existing is None:
        # Brand-new identity — defer creating the User row until
        # they pick a club (users.club_id is NOT NULL and never reopened;
        # see complete-signup). Carry the identity in a short-lived signed
        # cookie and send them to the frontend's club-picker.
        #
        # `next` rides along as a query param so it survives the club-picker
        # and the claim-profile step that follow. Without it a player who
        # followed a "sign up for Thursday" link out of Discord finished
        # onboarding on the club's front page with no idea why they were
        # there — which is exactly what happened to the Age of Sigmar players
        # on 09/09/2026. Built from `origin`, never from origin+next, or the
        # path lands on the end of a query string.
        # The club-request page is the one destination a brand-new identity can
        # use WITHOUT an account, so it skips the club picker entirely. That is
        # the whole point: the organiser asking for a club cannot pick one,
        # because users.club_id is NOT NULL and their club is what's missing.
        # The pending-signup cookie set below is enough for that page, and
        # provisioning turns it into a real User later.
        if next_path.startswith(CLUB_REQUEST_PATH):
            response = RedirectResponse(origin + next_path)
        else:
            query = {}
            if next_path:
                query["next"] = next_path
            # Decision C: a verified email already on another account is not
            # joined to it, but the person is told, so they can sign in the way
            # they usually do and add this from their account instead of
            # carrying on into a second one.
            if profile.email_verified and account_with_verified_email(db, profile.email, profile.subject):
                query["existing_account"] = "1"
            join_url = f"{origin}/join"
            if query:
                join_url += "?" + urlencode(query, quote_via=quote, safe="")
            response = RedirectResponse(join_url)
        response.set_cookie(
            "cta_pending_signup",
            _make_pending_profile_cookie(profile),
            max_age=600,  # 10 minutes: enough to pick a club, short enough not to linger
            httponly=True,
            samesite="lax",
            secure=True,
        )
        _clear_oauth_cookies(response)
        # find_user_for_profile may have healed a mirror-only account's identity
        # row; nothing else was written, but don't leave it half-done.
        db.commit()
        return response

    # Only provider-owned fields change on a returning sign-in; see
    # identity.record_sign_in. display_name is the user's and is never touched.
    record_sign_in(db, existing, profile)
    db.commit()
    db.refresh(existing)

    response = RedirectResponse(origin + next_path)
    _set_session_cookie(response, existing)
    _clear_oauth_cookies(response)
    return response


def _clear_oauth_cookies(response: Response) -> None:
    response.delete_cookie("cta_oauth_link")
    response.delete_cookie("cta_oauth_state")
    response.delete_cookie("cta_oauth_return_to")
    response.delete_cookie("cta_oauth_next")


def _user_out(user: User) -> dict:
    """The account as the browser sees it. Explicit, so a column added to
    users (an email, a token, session_version) never reaches the browser just
    by existing — /auth/me used to return the raw row."""
    return {
        "id": user.id,
        "name": display_name_for(user),
        "display_name": user.display_name,
        "discord_name": user.discord_name,
        "avatar_url": user.avatar_url,
        "player_id": user.player_id,
        "club_id": user.club_id,
        "home_club_id": user.home_club_id,
        "is_super_admin": user.is_super_admin,
        "is_platform_admin": user.is_platform_admin,
    }


@router.get("/me")
def me(
    request: Request,
    response: Response,
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Frontend calls this to ask "who am I logged in as?".

    Multi-club network model: "player" and "claim_candidates" are relative to
    the ACTIVE club (the subdomain the user is on, else their home club), not a
    single global player. A user with no player *at this club* gets the
    claim/create flow here even if they have a player at another club."""
    if user is None:
        return {"authenticated": False, "sign_in_providers": sign_in_providers()}

    # Roll the 30 days forward for someone who is still using the app. Every
    # page calls this, so an active session is always freshly stamped; one
    # that stops being used expires on its own. A cookie from before stamping
    # existed has no date, so it gets one now.
    parsed = _parse_session_cookie(session_cookie)
    issued = parsed[2] if parsed else None
    if issued is None or datetime.utcfromtimestamp(issued) < datetime.utcnow() - SESSION_REFRESH_AFTER:
        _set_session_cookie(response, user)

    active_club = resolve_active_club_id(db, user, request.headers.get("origin"))
    my_player_id = active_player_id_for(db, user, active_club)

    linked_player = db.get(Player, my_player_id) if my_player_id else None

    candidates = []
    if my_player_id is None:
        # Only players at the active club that nobody owns yet are claimable.
        # Archived rows included: the roster was cleared of everyone with no
        # linked account (15/09/2026), and a returning player who can't see
        # their row creates a second one and loses their history.
        candidates = db.exec(
            scoped(Player, active_club)
            .where(Player.user_id.is_(None))
            .order_by(Player.name)
        ).all()

    club = db.get(Club, active_club)

    # Does this user have a player at ANY club yet? Used by the frontend to send
    # a brand-new player to the club finder rather than a default club subdomain.
    # Ignores `active` for the same reason active_player_id_for does: an
    # archived player still belongs to a club, and sending them round the club
    # finder as if they were brand new is how duplicate rows got made.
    has_club = db.exec(
        select(Player).where(Player.user_id == user.id)
    ).first() is not None

    return {
        "authenticated": True,
        "sign_in_providers": sign_in_providers(),
        "user": _user_out(user),
        "player": linked_player,
        "has_club": has_club,
        "active_club": (
            {"id": club.id, "slug": club.slug, "name": club.name} if club else None
        ),
        "claim_candidates": [
            {"id": p.id, "name": p.name, "default_faction": p.default_faction}
            for p in candidates
        ],
    }


class CompleteSignupRequest(BaseModel):
    club_id: int


@router.post("/complete-signup")
def complete_signup(
    body: CompleteSignupRequest,
    response: Response,
    cta_pending_signup: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """Step 3 for a brand-new identity: the frontend's club-picker
    submits the chosen club, and the deferred User row (see
    _finish_sign_in's new-person branch) is created for real here.

    Race-safe: if an account for this identity already exists by the
    time this runs (double-submit, two tabs), don't create a duplicate —
    just log into the existing row, same idempotent spirit as
    admin.py's grant_role.
    """
    pending = _verify_pending_signup_cookie(cta_pending_signup)
    if pending is None:
        raise HTTPException(status_code=400, detail="No valid pending signup found. Please log in again.")

    club = db.get(Club, body.club_id)
    if club is None or not club.active:
        raise HTTPException(status_code=404, detail="Club not found.")

    user = find_user_for_profile(db, pending)
    if user is None:
        user = create_user_for_profile(
            db, pending,
            player_id=None,
            club_id=club.id,
            home_club_id=club.id,  # the club they picked is their soft home
        )
        if pending.provider == PASSWORD:
            # The hash has been waiting on the confirmation token since they
            # typed it (email_login.PASSWORD_SIGNUP).
            secret = email_login.claim_signup_secret(db, pending.subject)
            if secret:
                db.add(PasswordCredential(user_id=user.id, hash=secret))
    db.commit()
    db.refresh(user)

    # Brand-new identity: active club == the club they just picked (== home).
    my_player_id = active_player_id_for(db, user, club.id)
    linked_player = db.get(Player, my_player_id) if my_player_id else None

    candidates = []
    if my_player_id is None:
        candidates = db.exec(
            scoped(Player, club.id)
            .where(Player.user_id.is_(None))
            .order_by(Player.name)
        ).all()

    _set_session_cookie(response, user)
    response.delete_cookie("cta_pending_signup")

    return {
        "authenticated": True,
        "user": _user_out(user),
        "player": linked_player,
        "claim_candidates": [
            {"id": p.id, "name": p.name, "default_faction": p.default_faction}
            for p in candidates
        ],
    }


def _reject_if_already_linked(db: Session, user: User, club_id: int) -> None:
    """Guard for claim and create: one account, one player per club.

    Split out because the archived case needs a different message. Both paths
    used to say "You already have a linked player profile at this club", which
    is true and completely unhelpful to someone whose profile is archived —
    they can't see it on the roster, in the league, or anywhere else, so being
    told they have one reads as a bug. Before this guard even saw archived rows
    (active_player_id_for filtered them out) they'd sail past it and get a
    second, empty player. Now they're stopped, so the message has to explain
    the state they're actually in and who can change it.
    """
    existing_id = active_player_id_for(db, user, club_id)
    if existing_id is None:
        return
    existing = db.get(Player, existing_id)
    if existing is not None and not existing.active:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Your profile ({existing.name}) is archived, so it's hidden for now. "
                f"Ask a club admin to put you back on the roster. Your games, level and "
                f"league record are all still there."
            ),
        )
    raise HTTPException(
        status_code=400, detail="You already have a linked player profile at this club"
    )


@router.post("/claim/{player_id}")
def claim_player(
    player_id: int,
    club_id: int = Depends(active_club_id),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """User picks an existing player from the dropdown — link them, at the
    ACTIVE club. Multi-club network model: a user can own one player per club,
    so the "already linked" check is per-club, and ownership is recorded on
    Player.user_id (not the single User.player_id)."""
    _reject_if_already_linked(db, user, club_id)

    player = db.get(Player, player_id)
    if player is None or player.club_id != club_id:
        raise HTTPException(status_code=404, detail="Player not found")
    if player.user_id is not None:
        raise HTTPException(status_code=400, detail="That player is already claimed by another user")

    # Claiming an archived row is someone coming back, so it goes back on the
    # roster. Unclaimed rows are archived by default (see me() above).
    player.user_id = user.id
    player.active = True
    db.add(player)
    # Expand-phase dual-write: keep the legacy User.player_id link in sync for
    # the user's home club, so not-yet-converted code paths (signups/main/admin
    # still read user.player_id) keep working until the full sweep lands.
    if club_id == user.club_id and user.player_id is None:
        user.player_id = player.id
        db.add(user)
    db.commit()
    return {"ok": True, "player_id": player_id}


class CreateProfileRequest(BaseModel):
    name: str
    default_faction: Optional[str] = None


@router.post("/create-profile")
def create_profile(
    body: CreateProfileRequest,
    club_id: int = Depends(active_club_id),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Create a brand-new player row at the ACTIVE club and link it to the
    current user (Player.user_id). Used for people with no existing row to
    claim. Multi-club network model: a user can create one player per club.
    """
    _reject_if_already_linked(db, user, club_id)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be blank")

    player = Player(
        name=name,
        default_faction=body.default_faction or None,
        active=True,
        club_id=club_id,
        user_id=user.id,
    )
    db.add(player)
    if user.display_name is None and user.discord_name is None:
        # An account made with an email link has no name from anywhere, so the
        # name it just gave the roster becomes its account name, which it can
        # change on /account. Without this the app would greet it as "Player".
        try:
            user.display_name = clean_display_name(name[:32])
        except ValueError:
            pass
        db.add(user)
    db.flush()  # populate player.id before the legacy back-link

    # Expand-phase dual-write (see claim_player) — home club only.
    if club_id == user.club_id and user.player_id is None:
        user.player_id = player.id
        db.add(user)
    db.commit()
    db.refresh(player)
    return {"ok": True, "player_id": player.id}


@router.get("/account")
def account(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Everything the account page shows: the account, the ways into it, and
    the player it owns at each club.

    Only the signed-in account's own data. `can_add` lists the sign-in methods
    the app offers that this account doesn't have yet, which is what drives the
    "add another way to sign in" nudge; it is empty while Discord is the only
    method.
    """
    identities = db.exec(
        select(UserIdentity).where(UserIdentity.user_id == user.id)
        .order_by(UserIdentity.provider, UserIdentity.is_primary.desc(), UserIdentity.created_at)
    ).all()
    rows = db.exec(
        select(Player, Club).join(Club, Club.id == Player.club_id)
        .where(Player.user_id == user.id)
        .order_by(Club.name)
    ).all()
    have = {i.provider for i in identities}
    return {
        "user": {**_user_out(user), "created_at": user.created_at},
        "identities": [
            {
                "id": i.id,
                "provider": i.provider,
                "name": i.name,
                "avatar_url": i.avatar_url,
                "email": i.email,
                "email_verified": i.email_verified,
                "is_primary": i.is_primary,
                "created_at": i.created_at,
                "last_used_at": i.last_used_at,
            }
            for i in identities
        ],
        "players": [
            {
                "id": p.id,
                "name": p.name,
                "active": p.active,
                "club": {"id": c.id, "slug": c.slug, "name": c.name},
            }
            for p, c in rows
        ],
        "can_add": [p for p in linkable_providers() if p not in have],
        "has_password": db.exec(
            select(PasswordCredential).where(PasswordCredential.user_id == user.id)
        ).first() is not None,
    }


class AccountPatch(BaseModel):
    display_name: Optional[str] = None


@router.patch("/account")
def update_account(
    body: AccountPatch,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Set or clear the name the account goes by (account overhaul Slab 3).

    This is the account's name, shown when the app greets someone and beside
    their Discord handle in admin lists. It is NOT a club roster name:
    Player.name stays per club and admin-renamed, because the pairing engine
    keys players on it (ACCOUNT_OVERHAUL.md §3, Decision B). Sending null or
    blank clears it, and the account goes by its Discord handle again.
    """
    try:
        new_name = clean_display_name(body.display_name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    old_name = user.display_name
    user.display_name = new_name
    db.add(user)
    if new_name != old_name:
        # So an admin can see who a name belonged to, and when it changed.
        log_audit(db, user, "account.name", "user", user.id, f"{old_name!r} -> {new_name!r}")
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _user_out(user)}


def _identity_of(db: Session, user: User, identity_id: int) -> UserIdentity:
    ident = db.get(UserIdentity, identity_id)
    if ident is None or ident.user_id != user.id:
        raise HTTPException(status_code=404, detail="Sign-in method not found.")
    return ident


def _sync_discord_mirror(db: Session, user: User) -> None:
    """Point users.discord_id/discord_name/avatar_url at the account's primary
    Discord identity, or clear them if it has none. Keeps the name the account
    went by: someone losing their only Discord keeps it as their chosen name."""
    primary = identity_for(db, user.id, DISCORD)
    if primary is not None:
        user.discord_id = primary.provider_user_id
        user.discord_name = primary.name
        user.avatar_url = primary.avatar_url
    else:
        if user.display_name is None and user.discord_name:
            user.display_name = clean_display_name(user.discord_name[:32])
        user.discord_id = None
        user.discord_name = None
        user.avatar_url = next(
            (i.avatar_url for i in db.exec(select(UserIdentity).where(UserIdentity.user_id == user.id)).all()
             if i.avatar_url), None)
    db.add(user)


@router.delete("/identities/{identity_id}")
def remove_identity(
    identity_id: int,
    response: Response,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Remove a way of signing in. Never the last one. Ends every other session
    the account has, since any of them may have come in through the method
    being removed, and keeps this browser signed in with a fresh cookie."""
    ident = _identity_of(db, user, identity_id)
    remaining = [i for i in db.exec(select(UserIdentity).where(UserIdentity.user_id == user.id)).all()
                 if i.id != ident.id]
    if not remaining:
        raise HTTPException(status_code=409, detail="That's your only way to sign in, so it can't be removed.")
    provider, was_primary = ident.provider, ident.is_primary
    label = f"{provider} {ident.email or ident.name or ident.provider_user_id}"
    if provider == PASSWORD:
        for credential in db.exec(select(PasswordCredential)
                                  .where(PasswordCredential.user_id == user.id)).all():
            db.delete(credential)
    db.delete(ident)
    db.flush()
    if was_primary:
        successor = next((i for i in sorted(remaining, key=lambda i: i.created_at) if i.provider == provider), None)
        if successor is not None:
            successor.is_primary = True
            db.add(successor)
            db.flush()
    if provider == DISCORD:
        _sync_discord_mirror(db, user)
    bump_session_version(db, user)
    log_audit(db, user, "identity.unlink", "user", user.id, label)
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, user)
    return {"ok": True}


@router.post("/identities/{identity_id}/primary")
def make_identity_primary(
    identity_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Choose which of the account's accounts from one provider counts. For
    Discord that is the one club posts tag and the membership gate checks."""
    ident = _identity_of(db, user, identity_id)
    if not ident.is_primary:
        for other in db.exec(select(UserIdentity).where(UserIdentity.user_id == user.id)
                             .where(UserIdentity.provider == ident.provider)).all():
            if other.is_primary:
                other.is_primary = False
                db.add(other)
        db.flush()
        ident.is_primary = True
        db.add(ident)
        db.flush()
        if ident.provider == DISCORD:
            _sync_discord_mirror(db, user)
        log_audit(db, user, "identity.primary", "user", user.id, f"{ident.provider} {ident.name or ident.provider_user_id}")
        db.commit()
    return {"ok": True}


@router.get("/merge")
def merge_preview(
    user: User = Depends(require_user),
    cta_merge: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """What merging the offered account into this one would do, and whether it
    can be done."""
    drop_id = _merge_offer(db, cta_merge, user)
    if drop_id is None:
        raise HTTPException(status_code=404, detail="No merge to confirm. It may have expired.")
    other = db.get(User, drop_id)
    plan = user_merge.plan_merge(db, user.id, drop_id)
    players = db.exec(select(Player, Club).join(Club, Club.id == Player.club_id)
                      .where(Player.user_id == drop_id)).all()
    idents = db.exec(select(UserIdentity).where(UserIdentity.user_id == drop_id)).all()
    return {
        "other": {
            "name": display_name_for(other),
            "created_at": other.created_at,
            "identities": [{"provider": i.provider, "name": i.name, "email": i.email} for i in idents],
            "players": [{"name": p.name, "club": c.name} for p, c in players],
        },
        "problems": plan.problems,
    }


@router.post("/merge/confirm")
def merge_confirm(
    response: Response,
    user: User = Depends(require_user),
    cta_merge: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """Fold the offered account into this one (user_merge.merge_users)."""
    drop_id = _merge_offer(db, cta_merge, user)
    if drop_id is None:
        raise HTTPException(status_code=404, detail="No merge to confirm. It may have expired.")
    dropped_name = display_name_for(db.get(User, drop_id))
    try:
        user_merge.merge_users(db, user.id, drop_id)
    except user_merge.MergeRefused as e:
        db.rollback()
        raise HTTPException(status_code=409, detail="; ".join(e.problems))
    log_audit(db, user, "account.merge", "user", user.id, f"merged user {drop_id} ({dropped_name!r})")
    db.commit()
    response.delete_cookie("cta_merge")
    return {"ok": True}


@router.post("/merge/cancel")
def merge_cancel(response: Response):
    response.delete_cookie("cta_merge")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Email and password (Slab 8). What is stored, and why nobody can read a
# password out of it, is in passwords.py.
# ---------------------------------------------------------------------------

def _attempt_key(scope: str, value: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), f"{scope}:{value}".encode(), hashlib.sha256).hexdigest()


def _too_many_attempts(db: Session, scope: str, value: str, limit: int) -> bool:
    since = datetime.utcnow() - PASSWORD_FAIL_WINDOW
    hits = db.exec(
        select(LoginAttempt).where(LoginAttempt.key_hash == _attempt_key(scope, value))
        .where(LoginAttempt.created_at > since)
    ).all()
    return len(hits) >= limit


def _record_failure(db: Session, email: str, ip: str) -> None:
    """Remember a wrong password, and prune old rows as we go. Caller commits."""
    for scope, value in (("email", email), ("ip", ip)):
        db.add(LoginAttempt(scope=scope, key_hash=_attempt_key(scope, value)))
    if secrets.randbelow(20) == 0:
        cutoff = datetime.utcnow() - timedelta(days=1)
        for old in db.exec(select(LoginAttempt).where(LoginAttempt.created_at < cutoff)).all():
            db.delete(old)


def _clear_failures(db: Session, email: str) -> None:
    for row in db.exec(select(LoginAttempt).where(LoginAttempt.key_hash == _attempt_key("email", email))).all():
        db.delete(row)


def _password_identity(db: Session, email: str) -> Optional[UserIdentity]:
    return find_identity(db, PASSWORD, email)


def _set_password(db: Session, user: User, password: str) -> None:
    """Store a new password for `user`, replacing any it had. Caller commits."""
    row = db.exec(select(PasswordCredential).where(PasswordCredential.user_id == user.id)).first()
    hashed = passwords.hash_password(password)
    if row is None:
        db.add(PasswordCredential(user_id=user.id, hash=hashed))
    else:
        row.hash = hashed
        row.updated_at = datetime.utcnow()
        db.add(row)


class PasswordSignupBody(BaseModel):
    email: str
    password: str
    next: Optional[str] = None


_SIGNUP_SENT = {
    "ok": True,
    # Same words whether or not the address already has an account: the form
    # must not answer "does this person play here".
    "detail": "Check that inbox to confirm your address and finish. The link works for 15 minutes.",
}


@router.post("/password/signup")
def password_signup(body: PasswordSignupBody, request: Request, db: Session = Depends(get_session)):
    """Start an account with an email address and a password.

    Nothing is created here. The password is hashed and held against a
    confirmation link, so the account only exists once someone has read the
    email at that address.
    """
    if PASSWORD not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Signing up with a password isn't available.")
    email = email_login.normalise_email(body.email)
    if email is None:
        raise HTTPException(status_code=422, detail="That doesn't look like an email address.")
    try:
        passwords.check_strength(body.password, email)
    except passwords.PasswordRejected as e:
        raise HTTPException(status_code=422, detail=str(e))

    origin = _request_origin(request)
    ip = email_login.client_ip(request)
    taken = _password_identity(db, email) is not None or account_with_verified_email(db, email) is not None
    try:
        if taken:
            # Told by email, not here. Someone who already has an account gets a
            # way back in; someone probing the form learns nothing.
            token = None
        else:
            token = email_login.issue(db, email=email, purpose=email_login.PASSWORD_SIGNUP,
                                      origin=origin, ip=ip, next_path=_safe_next_path(body.next),
                                      secret=passwords.hash_password(body.password))
    except email_login.RateLimited:
        raise HTTPException(status_code=429, detail="That's a lot of attempts. Please wait a while and try again.")
    db.commit()
    if taken:
        _send_or_explain_notice(email, f"{origin}/signin")
    else:
        _send_or_explain(email, email_login.PASSWORD_SIGNUP, email_login.link_url(origin, token))
    return _SIGNUP_SENT


def _send_or_explain_notice(email: str, sign_in_url: str) -> None:
    try:
        email_login.send_existing_account_notice(email, sign_in_url)
    except UndeliverableRecipient:
        pass
    except RuntimeError as e:
        capture(e, where="existing-account notice")


class PasswordSignInBody(BaseModel):
    email: str
    password: str
    next: Optional[str] = None


_WRONG = "Email or password is incorrect."


@router.post("/password/signin")
def password_signin(
    body: PasswordSignInBody,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """Sign in with an email address and a password."""
    if PASSWORD not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Signing in with a password isn't available.")
    email = email_login.normalise_email(body.email) or ""
    ip = email_login.client_ip(request)
    if (_too_many_attempts(db, "email", email, PASSWORD_FAIL_LIMIT_EMAIL)
            or _too_many_attempts(db, "ip", ip, PASSWORD_FAIL_LIMIT_IP)):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Wait a few minutes, or use the forgotten password link.",
        )

    ident = _password_identity(db, email) if email else None
    credential = db.exec(
        select(PasswordCredential).where(PasswordCredential.user_id == ident.user_id)
    ).first() if ident else None
    user = db.get(User, ident.user_id) if ident else None
    ok, fresh = passwords.verify_password(credential.hash, body.password) if credential else (False, None)
    if not ok or user is None:
        _record_failure(db, email, ip)
        db.commit()
        raise HTTPException(status_code=401, detail=_WRONG)

    if fresh:
        # Hashed with weaker settings than today's; quietly bring it up to date.
        credential.hash = fresh
        credential.updated_at = datetime.utcnow()
        db.add(credential)
    _clear_failures(db, email)
    _touch_identity(db, ident)
    user.last_login_at = datetime.utcnow()
    db.add(user)
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, user)
    origin = _request_origin(request)
    return {"ok": True, "redirect": origin + _safe_next_path(body.next)}


def _touch_identity(db: Session, ident: UserIdentity) -> None:
    ident.last_used_at = datetime.utcnow()
    db.add(ident)


class PasswordForgotBody(BaseModel):
    email: str


@router.post("/password/forgot")
def password_forgot(body: PasswordForgotBody, request: Request, db: Session = Depends(get_session)):
    """Email a link to choose a new password. Says the same either way."""
    if PASSWORD not in sign_in_providers():
        raise HTTPException(status_code=404, detail="Signing in with a password isn't available.")
    email = email_login.normalise_email(body.email)
    if email is None:
        raise HTTPException(status_code=422, detail="That doesn't look like an email address.")
    origin = _request_origin(request)
    ident = _password_identity(db, email)
    token = None
    if ident is not None:
        try:
            token = email_login.issue(db, email=email, purpose=email_login.PASSWORD_RESET,
                                      origin=origin, ip=email_login.client_ip(request),
                                      user_id=ident.user_id)
        except email_login.RateLimited:
            raise HTTPException(status_code=429, detail="That's a lot of attempts. Please wait a while and try again.")
        db.commit()
    if token:
        _send_or_explain(email, email_login.PASSWORD_RESET, email_login.reset_url(origin, token))
    return {
        "ok": True,
        "detail": "If that address has an account with a password, a link to choose a new one is on its way.",
    }


class PasswordResetBody(BaseModel):
    token: str
    password: str


@router.post("/password/reset")
def password_reset(
    body: PasswordResetBody,
    response: Response,
    db: Session = Depends(get_session),
):
    """Choose a new password from an emailed link, and sign in.

    Ends every other session: this is what someone uses when they think
    somebody else has got into their account.
    """
    try:
        row = email_login.peek(db, body.token)
    except email_login.InvalidToken:
        raise HTTPException(status_code=410, detail="That link has expired or been used. Ask for a new one.")
    if row.purpose != email_login.PASSWORD_RESET or row.user_id is None:
        raise HTTPException(status_code=410, detail="That link has expired or been used. Ask for a new one.")
    user = db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status_code=410, detail="That link has expired or been used. Ask for a new one.")
    try:
        passwords.check_strength(body.password, row.email)
    except passwords.PasswordRejected as e:
        raise HTTPException(status_code=422, detail=str(e))

    email_login.spend(db, row)
    _set_password(db, user, body.password)
    # The 429 tells people to use this link, so it has to clear the lockout it
    # sent them here from. Without this, resetting the password left them
    # still shut out until the window passed.
    _clear_failures(db, row.email)
    ident = _password_identity(db, row.email)
    if ident is not None and not ident.email_verified:
        # Reading the email proved the address.
        ident.email_verified = True
        db.add(ident)
    bump_session_version(db, user)
    log_audit(db, user, "password.reset", "user", user.id, row.email)
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, user)
    return {"ok": True, "redirect": f"{row.origin}/account"}


class PasswordSetBody(BaseModel):
    password: str
    current_password: Optional[str] = None


@router.post("/password/set")
def password_set(
    body: PasswordSetBody,
    response: Response,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Set or change this account's password, from /account.

    Changing one asks for the current password and ends other sessions: if
    somebody else is signed in, a new password should shut them out.
    """
    if PASSWORD not in linkable_providers():
        raise HTTPException(status_code=404, detail="Passwords aren't available.")
    existing = db.exec(select(PasswordCredential).where(PasswordCredential.user_id == user.id)).first()
    if existing is not None:
        ok, _ = passwords.verify_password(existing.hash, body.current_password or "")
        if not ok:
            raise HTTPException(status_code=403, detail="That isn't your current password.")
    ident = db.exec(select(UserIdentity).where(UserIdentity.user_id == user.id)
                    .where(UserIdentity.provider == PASSWORD)).first()
    email = ident.provider_user_id if ident else None
    if ident is None:
        # A password needs an address to sign in with and to recover through.
        verified = db.exec(select(UserIdentity).where(UserIdentity.user_id == user.id)
                           .where(UserIdentity.email_verified == True)  # noqa: E712
                           .where(UserIdentity.email.is_not(None))).first()
        if verified is None:
            raise HTTPException(
                status_code=409,
                detail="Add a confirmed email address first, so you can sign in with it and get back in if you forget.",
            )
        email = verified.email
    try:
        passwords.check_strength(body.password, email)
    except passwords.PasswordRejected as e:
        raise HTTPException(status_code=422, detail=str(e))

    _set_password(db, user, body.password)
    if ident is None:
        attach_identity(db, user, ProviderProfile(provider=PASSWORD, subject=email, email=email,
                                                  email_verified=True))
        log_audit(db, user, "identity.link", "user", user.id, f"password {email}")
    if existing is not None:
        bump_session_version(db, user)
        log_audit(db, user, "password.change", "user", user.id, email or "")
    db.commit()
    db.refresh(user)
    _set_session_cookie(response, user)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    """Clear the session cookie. Returns JSON because the frontend calls this
    via fetch(); a redirect response would just confuse the fetch."""
    response.delete_cookie(
        "cta_session",
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return {"ok": True}


@router.post("/logout-everywhere")
def logout_everywhere(
    response: Response,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """End every session this account has, on every device, including this
    one. The primitive recovery and unlinking are built on."""
    bump_session_version(db, user)
    db.commit()
    response.delete_cookie(
        "cta_session",
        httponly=True,
        samesite="lax",
        secure=True,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin permission helpers
# ---------------------------------------------------------------------------

def valid_scopes(db: Session) -> set[str]:
    """The global whitelist of scope names that exist at all: every active
    SystemConfig catalogue row's legacy_system_name. There is no separate
    "League" scope — league admin (results/config/seasons) lives inside each
    system's own scope now (ClubSystem.league_enabled gates whether that
    system's league section is usable), not a standalone pseudo-scope.

    This is a global "is this even a real scope name anywhere" check, not
    per-club authorization — a scope can pass this and still be unusable
    for a specific club (see club_runnable_scopes). Replaces the old
    hardcoded VALID_SCOPES frozenset, which went stale the moment the
    system catalogue became editable via POST /admin/platform/systems."""
    return {
        sc.legacy_system_name
        for sc in db.exec(select(SystemConfig).where(SystemConfig.active == True)).all()
    }


def club_runnable_scopes(club_id: int, db: Session) -> set[str]:
    """The scopes a given club can actually administer: its enabled
    ClubSystem rows' legacy_system_name. (League admin is folded into each
    system's own scope, gated within by ClubSystem.league_enabled — not a
    separate scope name.) Distinct from valid_scopes() (a global
    format/existence whitelist) — this is per-club authorization, used both
    for super-admins' implicit scope set (admin_scopes) and to validate
    POST /admin/roles grants."""
    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id, ClubSystem.enabled == True)
    ).all()
    return {sc.legacy_system_name for _, sc in rows}


def admin_scopes(user: Optional[User], db: Session, club_id: Optional[int] = None) -> set[str]:
    """Return the set of scopes the user can administer AT A GIVEN CLUB.

    `club_id` defaults to the user's own registration club (backward
    compatible with every caller that predates the multi-club network model).
    Pass an explicit club_id — e.g. the active club from the subdomain — to
    ask "what may this user administer *here*".

    Super-admin is a home-club power: it grants that club's runnable scopes
    ONLY when the club being asked about is the user's own club. At any other
    club a super-admin has exactly the admin_roles they were explicitly granted
    there (normally none) — so a super-admin of one club is a plain player at
    every other club, never an admin-by-accident of the whole network.
    Regular users get whatever admin_roles rows they hold for that club.
    """
    if user is None:
        return set()
    cid = club_id if club_id is not None else user.club_id
    # Platform admins are implicit super-admins of EVERY club — full runnable
    # scopes wherever they act. This is not stored as a role and not tied to a
    # club, so a platform admin never appears in a club's super-admin list and
    # is never removed when that club appoints/removes its own admins.
    if user.is_platform_admin:
        return club_runnable_scopes(cid, db)
    if user.is_super_admin and cid == user.club_id:
        return club_runnable_scopes(cid, db)
    rows = db.exec(scoped(AdminRole, cid).where(AdminRole.user_id == user.id)).all()
    return {r.scope for r in rows}


def _rebase_admin(user: User, active_club: int, db: Session) -> User:
    """Re-base an already-authorized admin's user object onto the club they're
    acting in (the active club from the subdomain), so every downstream
    scoped(X, user.club_id) read and club_id=user.club_id write in the endpoint
    targets THAT club. This is what lets a platform admin administer whatever
    club they switch to, while a club's own super-admin stays on their own club
    (for them active always == their club, or they'd have been 403'd).

    A platform admin is additionally marked is_super_admin in-memory, so the
    handful of endpoints that gate on the raw `user.is_super_admin` flag (rather
    than admin_scopes) treat them as the super-admin they effectively are here.
    A real super-admin is unchanged; a scope-admin is NOT elevated.

    Detached from the session first so these overrides are request-only and
    never persisted: User has no ORM relationships (safe to detach) and no admin
    endpoint re-adds or re-fetches the caller (verified), so nothing flushes the
    changed fields back to the DB. Crucially this only touches the in-memory
    caller object — DB queries about OTHER users (e.g. a club's super-admin
    list) read the real columns, so a platform admin stays hidden there."""
    db.expunge(user)
    user.club_id = active_club
    if user.is_platform_admin:
        user.is_super_admin = True
    return user


def require_super_admin(
    user: User = Depends(require_user),
    active: int = Depends(active_club_id),
    db: Session = Depends(get_session),
) -> User:
    """Dependency: raises 403 unless the caller may act as a super-admin in the
    club they're currently in (the active club, from the subdomain).

    A platform admin is an implicit super-admin of every club, so passes
    anywhere. A club's own super-admin passes only in their own club (active ==
    user.club_id) — on another club's subdomain they're a plain player. The
    authorized caller is then re-based onto the active club so the endpoint's
    scoped(X, user.club_id) queries act on the club being administered."""
    if not (user.is_platform_admin or (user.is_super_admin and active == user.club_id)):
        raise HTTPException(status_code=403, detail="Super-admin access required.")
    return _rebase_admin(user, active, db)


def require_platform_admin(user: User = Depends(require_user)) -> User:
    """Dependency: raises 403 unless the caller is a platform admin.

    Platform admins can act across clubs (e.g. creating new clubs).
    Set by SQL, never via this API — same pattern as is_super_admin.
    """
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform-admin access required.")
    return user


def require_scope(scope: str):
    """Factory: returns a dependency that 403s unless the caller holds that
    scope in the club they're currently in (the active club, from the
    subdomain). A platform admin holds every scope everywhere; a club's own
    scope-admin holds theirs only in their own club. The authorized caller is
    re-based onto the active club so the endpoint acts on it. See
    require_super_admin."""
    def _dep(
        user: User = Depends(require_user),
        db: Session = Depends(get_session),
        active: int = Depends(active_club_id),
    ) -> User:
        if scope not in admin_scopes(user, db, active):
            raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")
        return _rebase_admin(user, active, db)
    return _dep


def require_admin(
    user: User = Depends(require_user),
    active: int = Depends(active_club_id),
    db: Session = Depends(get_session),
) -> User:
    """Dependency: raises 403 unless the caller can administer the club they're
    currently in (any scope at the active club — a platform admin qualifies at
    every club). The authorized caller is re-based onto the active club so the
    endpoint's scoped(X, user.club_id) queries act on it. The shared base gate
    for club-admin endpoints across modules (admin.py aliases it as
    _require_any_admin; analytics.py uses it directly). Endpoints needing a
    finer check still call their own per-system scope check on top."""
    if not admin_scopes(user, db, active):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return _rebase_admin(user, active, db)
