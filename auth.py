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

from database import (
    active_player_id_for, get_session, resolve_active_club_id,
    resolve_request_club_id, scoped,
)
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
    return_to = _safe_return_to(request) + _safe_next_path(next)
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
def me(
    request: Request,
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Frontend calls this to ask "who am I logged in as?".

    Multi-club network model: "player" and "claim_candidates" are relative to
    the ACTIVE club (the subdomain the user is on, else their home club), not a
    single global player. A user with no player *at this club* gets the
    claim/create flow here even if they have a player at another club."""
    if user is None:
        return {"authenticated": False}

    active_club = resolve_active_club_id(db, user, request.headers.get("origin"))
    my_player_id = active_player_id_for(db, user, active_club)

    linked_player = db.get(Player, my_player_id) if my_player_id else None

    candidates = []
    if my_player_id is None:
        # Only players at the active club that nobody owns yet are claimable.
        candidates = db.exec(
            scoped(Player, active_club)
            .where(Player.active == True, Player.user_id.is_(None))
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
        "user": user,
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
            home_club_id=club.id,  # the club they picked is their soft home
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # Brand-new identity: active club == the club they just picked (== home).
    my_player_id = active_player_id_for(db, user, club.id)
    linked_player = db.get(Player, my_player_id) if my_player_id else None

    candidates = []
    if my_player_id is None:
        candidates = db.exec(
            scoped(Player, club.id)
            .where(Player.active == True, Player.user_id.is_(None))
            .order_by(Player.name)
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
                f"Ask a club admin to put you back on the roster — your games, level and "
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
    if player is None or not player.active or player.club_id != club_id:
        raise HTTPException(status_code=404, detail="Player not found")
    if player.user_id is not None:
        raise HTTPException(status_code=400, detail="That player is already claimed by another user")

    player.user_id = user.id
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
    db.flush()  # populate player.id before the legacy back-link

    # Expand-phase dual-write (see claim_player) — home club only.
    if club_id == user.club_id and user.player_id is None:
        user.player_id = player.id
        db.add(user)
    db.commit()
    db.refresh(player)
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
