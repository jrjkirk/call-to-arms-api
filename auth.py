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
That means no session table to manage; logout is just "clear the cookie".
"""
import hmac
import hashlib
import os
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from database import get_session
from models import User, Player

# Read config from env. These are set as Fly secrets in production
# and in .env locally.
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

DISCORD_API = "https://discord.com/api"
SCOPES = "identify"  # we only want their username + avatar, nothing else

router = APIRouter(prefix="/auth", tags=["auth"])


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
    # Discord requires we redirect back to a URL we pre-registered in the
    # Developer Portal. This must match exactly.
    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "prompt": "none",  # if they're already authorised, skip the "Allow?" screen
    }
    auth_url = f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"

    response = RedirectResponse(auth_url)
    # Stash the state value briefly so we can check it matches in callback.
    # Short max_age (5 min) so stale logins don't linger.
    response.set_cookie("cta_oauth_state", state, max_age=300, httponly=True, samesite="lax")
    return response


@router.get("/discord/callback")
async def discord_callback(
    code: str,
    state: str,
    cta_oauth_state: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_session),
):
    """Step 2: Discord redirected back with a ?code= — exchange it for a user."""
    if not cta_oauth_state or cta_oauth_state != state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    redirect_uri = f"{BACKEND_URL}/auth/discord/callback"

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Exchange the one-time code for an access token
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

        # Now fetch the Discord user's profile
        user_resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch Discord user")
        discord_user = user_resp.json()

    discord_id = discord_user["id"]
    # Discord supports both old-style "user#1234" and new global usernames.
    # Prefer global_name if set; fall back to username.
    discord_name = discord_user.get("global_name") or discord_user.get("username", "Unknown")
    avatar_hash = discord_user.get("avatar")
    avatar_url = (
        f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png"
        if avatar_hash else None
    )

    # Upsert the users row
    existing = db.exec(select(User).where(User.discord_id == discord_id)).first()
    if existing:
        existing.discord_name = discord_name
        existing.avatar_url = avatar_url
        existing.last_login_at = datetime.utcnow()
        db.add(existing)
        user = existing
    else:
        user = User(
            discord_id=discord_id,
            discord_name=discord_name,
            avatar_url=avatar_url,
            player_id=None,
        )
        db.add(user)
    db.commit()
    db.refresh(user)

    # Issue the session cookie and redirect back to the frontend
    cookie_value = _make_session_cookie(user.id)
    response = RedirectResponse(FRONTEND_URL)
    response.set_cookie(
        "cta_session",
        cookie_value,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="lax",
        secure=BACKEND_URL.startswith("https://"),
    )
    # Clean up the short-lived OAuth state cookie
    response.delete_cookie("cta_oauth_state")
    return response


@router.get("/me")
def me(user: Optional[User] = Depends(current_user), db: Session = Depends(get_session)):
    """Frontend calls this to ask "who am I logged in as?"

    Returns the user row, plus the linked player (if claimed), and a list of
    candidate players for the claim-profile screen (when user.player_id is None).
    """
    if user is None:
        return {"authenticated": False}

    linked_player = None
    if user.player_id:
        linked_player = db.get(Player, user.player_id)

    candidates = []
    if user.player_id is None:
        # Show every active player as a candidate for claim-profile
        candidates = db.exec(
            select(Player).where(Player.active == True).order_by(Player.name)
        ).all()

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
    """User picks an existing player from the dropdown — link them.

    Refuses if this user already has a linked player, or if that player is
    already claimed by a different Discord user. Either case warrants
    admin attention, not silent overwrite.
    """
    if user.player_id is not None:
        raise HTTPException(status_code=400, detail="You already have a linked player profile")

    other = db.exec(select(User).where(User.player_id == player_id)).first()
    if other is not None:
        raise HTTPException(status_code=400, detail="That player is already claimed by another user")

    player = db.get(Player, player_id)
    if player is None or not player.active:
        raise HTTPException(status_code=404, detail="Player not found")

    user.player_id = player_id
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "player_id": player_id}


@router.post("/logout")
def logout():
    """Clear the session cookie."""
    response = RedirectResponse(FRONTEND_URL, status_code=303)
    response.delete_cookie("cta_session")
    return response