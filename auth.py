"""Discord OAuth2 + session cookie management.

Flow:
  1. /auth/discord/login    -- redirect user to Discord's authorize URL
  2. /auth/discord/callback -- Discord redirects back here with ?code=...
                               we exchange code for token, fetch the user's
                               Discord identity, upsert the users row, set a
                               session cookie, redirect to frontend
  3. /auth/me               -- frontend uses this to ask "who am I logged in as?"
  4. /auth/logout           -- clear the cookie

Sessions are stateless: the cookie value is `{user_id}.{hmac-signature}`.
We trust the cookie iff the signature verifies with our SESSION_SECRET.

COOKIE NOTE: cta_session, cta_oauth_state, cta_oauth_return_to, and
cta_pending_signup all currently use samesite="lax" + secure=True.
Chrome/Firefox treat http://localhost as trustworthy, so secure=True
still works for local dev.

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
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from database import get_session, scoped
from models import Club, ClubSystem, SystemConfig, User, Player, AdminRole

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

DISCORD_API = "https://discord.com/api"
SCOPES = "identify"

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


def _make_session_cookie(user_id: int) -> str:
    """Return 'user_id.signature' which the browser stores as the session cookie."""
    body = str(user_id)
    return f"{body}.{_sign(body)}"


def _verify_session_cookie(raw: str) -> Optional[int]:
    """If the cookie is valid and untampered, return the user_id. Otherwise None."""
    if not raw or "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        return int(body)
    except ValueError:
        return None


def _make_pending_signup_cookie(discord_id: str, discord_name: str, avatar_url: Optional[str]) -> str:
    """Same 'body.signature' shape as the session cookie, but the body is a
    base64-encoded JSON payload carrying the Discord identity for a
    brand-new user (see discord_callback's new-user branch) — enough to
    create the real User row later in complete-signup without re-hitting
    Discord's API."""
    payload = json.dumps({
        "discord_id": discord_id,
        "discord_name": discord_name,
        "avatar_url": avatar_url,
    })
    body = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{body}.{_sign(body)}"


def _verify_pending_signup_cookie(raw: Optional[str]) -> Optional[dict]:
    """If the cookie is valid and untampered, return the decoded payload dict.
    Otherwise None (missing, signature mismatch, or malformed body)."""
    if not raw or "." not in raw:
        return None
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("discord_id"):
        return None
    return payload


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


def current_user(
    session_cookie: Optional[str] = Cookie(default=None, alias="cta_session"),
    db: Session = Depends(get_session),
) -> Optional[User]:
    """Resolve the current user from the session cookie, or None if not logged in."""
    if not session_cookie:
        return None
    user_id = _verify_session_cookie(session_cookie)
    if user_id is None:
        return None
    return db.get(User, user_id)


def require_user(user: Optional[User] = Depends(current_user)) -> User:
    """Like current_user, but raise 401 if not authenticated."""
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@router.get("/discord/login")
def discord_login(request: Request):
    """Step 1: send the browser to Discord's authorize page."""
    if not DISCORD_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Discord OAuth is not configured")

    state = secrets.token_urlsafe(24)
    return_to = _safe_return_to(request)
    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"

    response = RedirectResponse(auth_url)
    response.set_cookie("cta_oauth_state", state, max_age=300, httponly=True, samesite="lax")
    response.set_cookie("cta_oauth_return_to", return_to, max_age=300, httponly=True, samesite="lax")
    return response


@router.get("/discord/callback")
async def discord_callback(
    code: str,
    state: str,
    cta_oauth_state: Optional[str] = Cookie(default=None),
    cta_oauth_return_to: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """Step 2: Discord redirected back with a ?code= — exchange it for a user."""
    if not cta_oauth_state or cta_oauth_state != state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

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
    discord_name = discord_user.get("global_name") or discord_user.get("username", "Unknown")
    avatar_hash = discord_user.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
        if avatar_hash else None
    )

    existing = db.exec(select(User).where(User.discord_id == discord_id)).first()
    return_to = cta_oauth_return_to or FRONTEND_URL

    if existing is None:
        # Brand-new Discord identity — defer creating the User row until
        # they pick a club (users.club_id is NOT NULL and never reopened;
        # see complete-signup). Carry the identity in a short-lived signed
        # cookie and send them to the frontend's club-picker.
        response = RedirectResponse(f"{return_to}/join")
        response.set_cookie(
            "cta_pending_signup",
            _make_pending_signup_cookie(discord_id, discord_name, avatar_url),
            max_age=600,  # 10 minutes: enough to pick a club, short enough not to linger
            httponly=True,
            samesite="lax",
            secure=True,
        )
        response.delete_cookie("cta_oauth_state")
        response.delete_cookie("cta_oauth_return_to")
        return response

    existing.discord_name = discord_name
    existing.avatar_url = avatar_url
    existing.last_login_at = datetime.utcnow()
    db.add(existing)
    db.commit()
    db.refresh(existing)

    cookie_value = _make_session_cookie(existing.id)
    response = RedirectResponse(return_to)
    response.set_cookie(
        "cta_session",
        cookie_value,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
        secure=True,
    )
    response.delete_cookie("cta_oauth_state")
    response.delete_cookie("cta_oauth_return_to")
    return response


@router.get("/me")
def me(user: Optional[User] = Depends(current_user), db: Session = Depends(get_session)):
    """Frontend calls this to ask "who am I logged in as?"."""
    if user is None:
        return {"authenticated": False}

    linked_player = None
    if user.player_id:
        linked_player = db.get(Player, user.player_id)

    candidates = []
    if user.player_id is None:
        candidates = db.exec(
            scoped(Player, user.club_id).where(Player.active == True).order_by(Player.name)
        ).all()

    club = db.get(Club, user.club_id)

    return {
        "authenticated": True,
        "user": user,
        "player": linked_player,
        "claim_candidates": [
            {"id": p.id, "name": p.name, "default_faction": p.default_faction}
            for p in candidates
        ],
        # Lets the frontend gate league-only UI (nav tab, etc.) on the
        # caller's own club instead of a hardcoded club id.
        "leagues_enabled": club.leagues_enabled if club else False,
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
    """Step 3 for a brand-new Discord identity: the frontend's club-picker
    submits the chosen club, and the deferred User row (see
    discord_callback's new-user branch) is created for real here.

    Race-safe: if a User row for this discord_id already exists by the
    time this runs (double-submit, two tabs), don't create a duplicate —
    just log into the existing row, same idempotent spirit as
    admin.py's grant_role.
    """
    pending = _verify_pending_signup_cookie(cta_pending_signup)
    if pending is None:
        raise HTTPException(status_code=400, detail="No valid pending signup found. Please log in again.")

    discord_id = pending["discord_id"]

    club = db.get(Club, body.club_id)
    if club is None or not club.active:
        raise HTTPException(status_code=404, detail="Club not found.")

    user = db.exec(select(User).where(User.discord_id == discord_id)).first()
    if user is None:
        user = User(
            discord_id=discord_id,
            discord_name=pending["discord_name"],
            avatar_url=pending.get("avatar_url"),
            player_id=None,
            club_id=club.id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    linked_player = None
    if user.player_id:
        linked_player = db.get(Player, user.player_id)

    candidates = []
    if user.player_id is None:
        candidates = db.exec(
            scoped(Player, user.club_id).where(Player.active == True).order_by(Player.name)
        ).all()

    response.set_cookie(
        "cta_session",
        _make_session_cookie(user.id),
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
        secure=True,
    )
    response.delete_cookie("cta_pending_signup")

    return {
        "authenticated": True,
        "user": user,
        "player": linked_player,
        "claim_candidates": [
            {"id": p.id, "name": p.name, "default_faction": p.default_faction}
            for p in candidates
        ],
    }


@router.post("/claim/{player_id}")
def claim_player(
    player_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """User picks an existing player from the dropdown — link them."""
    if user.player_id is not None:
        raise HTTPException(status_code=400, detail="You already have a linked player profile")

    other = db.exec(select(User).where(User.player_id == player_id)).first()
    if other is not None:
        raise HTTPException(status_code=400, detail="That player is already claimed by another user")

    player = db.get(Player, player_id)
    if player is None or not player.active or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found")

    user.player_id = player_id
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "player_id": player_id}


class CreateProfileRequest(BaseModel):
    name: str
    default_faction: Optional[str] = None


@router.post("/create-profile")
def create_profile(
    body: CreateProfileRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Create a brand-new player row and link it to the current user.

    Used for people who have never played before (no existing row to claim).
    """
    if user.player_id is not None:
        raise HTTPException(status_code=400, detail="You already have a linked player profile")

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name cannot be blank")

    player = Player(name=name, default_faction=body.default_faction or None, active=True, club_id=user.club_id)
    db.add(player)
    db.flush()  # populate player.id before linking

    user.player_id = player.id
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "player_id": player.id}


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


# ---------------------------------------------------------------------------
# Admin permission helpers
# ---------------------------------------------------------------------------

def valid_scopes(db: Session) -> set[str]:
    """The global whitelist of scope names that exist at all: every active
    SystemConfig catalogue row's legacy_system_name, plus "League" (which
    stays its own separate, already-working toggle via Club.leagues_enabled
    — not part of the SystemConfig catalogue, see club_runnable_scopes).

    This is a global "is this even a real scope name anywhere" check, not
    per-club authorization — a scope can pass this and still be unusable
    for a specific club (see club_runnable_scopes). Replaces the old
    hardcoded VALID_SCOPES frozenset, which went stale the moment the
    system catalogue became editable via POST /admin/platform/systems."""
    names = {
        sc.legacy_system_name
        for sc in db.exec(select(SystemConfig).where(SystemConfig.active == True)).all()
    }
    names.add("League")
    return names


def club_runnable_scopes(club_id: int, db: Session) -> set[str]:
    """The scopes a given club can actually administer: its enabled
    ClubSystem rows' legacy_system_name, plus "League" if the club has
    leagues_enabled. Distinct from valid_scopes() (a global format/existence
    whitelist) — this is per-club authorization, used both for
    super-admins' implicit scope set (admin_scopes) and to validate
    POST /admin/roles grants."""
    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id, ClubSystem.enabled == True)
    ).all()
    scopes = {sc.legacy_system_name for _, sc in rows}
    club = db.get(Club, club_id)
    if club is not None and club.leagues_enabled:
        scopes.add("League")
    return scopes


def admin_scopes(user: Optional[User], db: Session) -> set[str]:
    """Return the set of scopes the user can administer.

    Super-admins get whatever their own club actually runs (see
    club_runnable_scopes) — not every scope that exists platform-wide.
    Regular users get whatever admin_roles rows they hold. Unauthenticated
    callers get the empty set.
    """
    if user is None:
        return set()
    if user.is_super_admin:
        return club_runnable_scopes(user.club_id, db)
    rows = db.exec(scoped(AdminRole, user.club_id).where(AdminRole.user_id == user.id)).all()
    return {r.scope for r in rows}


def require_super_admin(user: User = Depends(require_user)) -> User:
    """Dependency: raises 403 unless the caller is a super-admin."""
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="Super-admin access required.")
    return user


def require_platform_admin(user: User = Depends(require_user)) -> User:
    """Dependency: raises 403 unless the caller is a platform admin.

    Platform admins can act across clubs (e.g. creating new clubs).
    Set by SQL, never via this API — same pattern as is_super_admin.
    """
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform-admin access required.")
    return user


def require_scope(scope: str):
    """Factory: returns a dependency that 403s unless the caller holds that scope."""
    def _dep(user: User = Depends(require_user), db: Session = Depends(get_session)) -> User:
        if scope not in admin_scopes(user, db):
            raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")
        return user
    return _dep
