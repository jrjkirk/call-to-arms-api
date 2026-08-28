"""Call-out endpoints: ad-hoc "call to arms".

A player who can't make regular club night posts an open request for a game
at a specific place/date/time. It's per (club, system): only that club's
players see it and only that system's Discord channel is notified. It stays
open until someone takes it up or its game time passes; the creator can
cancel it. A daily Discord reminder + auto-expiry is handled out-of-band by
scripts/run_call_outs_check.py.

Deliberately mirrors the pre-arranged-game flow in signups.py (same system/
club validation, vibe normalisation, per-system Discord webhook) — reusing
its helpers rather than re-deriving them.
"""
import os
from datetime import datetime
from urllib.parse import quote
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, SQLModel, select

from database import active_player_id_for, get_session, name_with_mention, scoped
from models import CallOut, Club, User
from auth import active_club_id, require_user
from signups import (
    _get_system_config,
    _require_system_enabled,
    _effective_vibe_config,
    _require_linked_player,
    require_discord_member,
    _post_webhook,
)

router = APIRouter(prefix="/call-outs", tags=["call-outs"])

APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")
_UK = ZoneInfo("Europe/London")

# Cap free-text so a stray paste can't bloat a Discord message or the DB.
_MAX_NOTES = 500


def now_uk_naive() -> datetime:
    """Current UK-local wall-clock time as a naive datetime, matching how
    CallOut.game_at is stored (see the model docstring)."""
    return datetime.now(_UK).replace(tzinfo=None)


class CallOutIn(SQLModel):
    """Request body for POST /call-outs. Creator identity comes from the
    session (the caller's player at the active club)."""
    system: str
    game_date: str          # YYYY-MM-DD (UK local)
    game_time: str          # HH:MM (UK local, 24h)
    vibe: Optional[str] = None
    faction: Optional[str] = None
    points: Optional[int] = None
    notes: Optional[str] = None


def _parse_game_at(game_date: str, game_time: str) -> datetime:
    try:
        return datetime.strptime(f"{game_date.strip()} {game_time.strip()}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=422, detail="Please give a valid date and time for the game.")


def _format_when(game_at: datetime) -> str:
    """Human-friendly UK-local rendering for Discord/UI, e.g. 'Sat 02 Aug, 18:30'."""
    return game_at.strftime("%a %d %b, %H:%M")


def _serialize(c: CallOut, my_player_id: Optional[int]) -> dict:
    return {
        "id": c.id,
        "system": c.system,
        "creator_player_id": c.creator_player_id,
        "creator_name": c.creator_name,
        "game_at": c.game_at.isoformat(),
        "game_date": c.game_at.strftime("%Y-%m-%d"),
        "game_time": c.game_at.strftime("%H:%M"),
        "when_label": _format_when(c.game_at),
        "vibe": c.vibe,
        "faction": c.faction,
        "points": c.points,
        "notes": c.notes,
        "status": c.status,
        "taker_name": c.taker_name,
        "is_mine": my_player_id is not None and c.creator_player_id == my_player_id,
    }


def _post_call_out(db: Session, club_id: int, system: str, content: str) -> None:
    """Post to the club's call-outs channel, or its signup channel if it hasn't
    split them out. Call-outs shared the signup webhook for their whole life,
    so the fallback is what stops every existing club going quiet the moment
    a separate channel becomes possible."""
    _post_webhook(
        db, club_id, system, content,
        webhook_type="call_outs", fallback_type="signup",
    )


def _call_out_link(db: Session, club_id: int, c: CallOut) -> str:
    """Deep link to this one call-out, on the club's own subdomain.

    The anchor is what makes it an "accept the game" link rather than a link to
    a page: the signup page gives every call-out card an id, so the browser
    lands on the row with the Take it up button rather than at the top of a
    page the reader then has to search.
    """
    from database import club_app_url
    club = db.get(Club, club_id)
    path = f"/signup?system={quote(system_label(c))}#call-out-{c.id}"
    if club is None:
        return f"{APP_PUBLIC_URL}{path}" if APP_PUBLIC_URL else ""
    return club_app_url(club, path)


def system_label(c: CallOut) -> str:
    """The system name the signup page expects in its ?system= param."""
    return c.system


def _webhook_content(c: CallOut, header: str, link: str = "") -> str:
    detail_parts = []
    if c.faction:
        detail_parts.append(f"⚔️ {c.faction}")
    if c.vibe:
        detail_parts.append(f"🎭 {c.vibe}")
    if c.points is not None:
        detail_parts.append(f"🛡️ {c.points} pts")
    lines = [
        f"{header}",
        f"🗓️ {_format_when(c.game_at)}",
    ]
    if detail_parts:
        lines.append(" • ".join(detail_parts))
    if c.notes:
        lines.append(f"📝 {c.notes}")
    if link:
        lines.append(f"➡️ Take it up in the app: {link}")
    elif APP_PUBLIC_URL:
        lines.append(f"➡️ Take it up in the app: {APP_PUBLIC_URL}")
    return "\n".join(lines)


@router.get("")
def list_call_outs(
    system: str,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Open, not-yet-expired call-outs for this club + system, soonest first."""
    now = now_uk_naive()
    rows = db.exec(
        scoped(CallOut, club_id)
        .where(CallOut.system == system)
        .where(CallOut.status == "open")
        .where(CallOut.game_at > now)
        .order_by(CallOut.game_at)
    ).all()
    my_player_id = active_player_id_for(db, user, club_id)
    return {"call_outs": [_serialize(c, my_player_id) for c in rows]}


@router.post("")
def create_call_out(
    body: CallOutIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    _require_system_enabled(db, club_id, body.system)

    player = _require_linked_player(user, db, club_id)
    require_discord_member(db, player, club_id, body.system)

    game_at = _parse_game_at(body.game_date, body.game_time)
    if game_at <= now_uk_naive():
        raise HTTPException(status_code=422, detail="The game's date and time must be in the future.")

    # Normalise per system, exactly like submit_prearranged.
    eff_vibe_options, eff_default_vibe = _effective_vibe_config(db, club_id, config)
    vibe = body.vibe if body.vibe in (eff_vibe_options or []) else eff_default_vibe
    points = max(0, min(int(body.points or config.default_points), config.max_points)) if config.uses_points else None
    faction = (body.faction or "").strip() or None
    notes = ((body.notes or "").strip() or None)
    if notes:
        notes = notes[:_MAX_NOTES]

    now = now_uk_naive()
    call_out = CallOut(
        club_id=club_id,
        system=body.system,
        creator_player_id=player.id,
        creator_name=player.name,
        game_at=game_at,
        vibe=vibe,
        faction=faction,
        points=points,
        notes=notes,
        status="open",
        # Seed the reminder clock so the first daily nudge is ~24h out, not
        # on the next hourly cron tick right after this initial post.
        last_reminder_at=now,
    )
    db.add(call_out)
    db.commit()
    db.refresh(call_out)

    try:
        _post_call_out(db, club_id, body.system, _webhook_content(
            call_out,
            f"📣 **Call Out!** {name_with_mention(db, call_out.creator_name, call_out.creator_player_id)} is looking for a game",
            _call_out_link(db, club_id, call_out),
        ))
    except Exception:
        pass

    return _serialize(call_out, player.id)


def _get_owned_call_out(db: Session, call_out_id: int, club_id: int) -> CallOut:
    """Fetch a call-out that belongs to the caller's active club, else 404 —
    never leak another club's call-out by id."""
    c = db.get(CallOut, call_out_id)
    if c is None or c.club_id != club_id:
        raise HTTPException(status_code=404, detail="Call-out not found.")
    return c


@router.post("/{call_out_id}/take")
def take_call_out(
    call_out_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    taker = _require_linked_player(user, db, club_id)
    # The call-out is loaded BEFORE the gate now: which Discord server applies
    # depends on which system the call-out is for, and only the row knows that.
    c = _get_owned_call_out(db, call_out_id, club_id)
    require_discord_member(db, taker, club_id, c.system)

    if c.status != "open":
        raise HTTPException(status_code=409, detail="This call-out is no longer open.")
    if c.game_at <= now_uk_naive():
        c.status = "expired"
        c.updated_at = now_uk_naive()
        db.add(c)
        db.commit()
        raise HTTPException(status_code=409, detail="This call-out has expired.")
    if c.creator_player_id == taker.id:
        raise HTTPException(status_code=409, detail="You can't take up your own call-out.")

    c.status = "taken"
    c.taker_player_id = taker.id
    c.taker_name = taker.name
    c.taken_at = now_uk_naive()
    c.updated_at = c.taken_at
    db.add(c)
    db.commit()
    db.refresh(c)

    try:
        header = (
            f"✅ **Call Out taken!** {name_with_mention(db, taker.name, taker.id)} "
            f"is playing {name_with_mention(db, c.creator_name, c.creator_player_id)}"
        )
        _post_call_out(db, club_id, c.system, _webhook_content(c, header))
    except Exception:
        pass

    return _serialize(c, taker.id)


@router.post("/{call_out_id}/cancel")
def cancel_call_out(
    call_out_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    c = _get_owned_call_out(db, call_out_id, club_id)
    my_player_id = active_player_id_for(db, user, club_id)
    if my_player_id is None or c.creator_player_id != my_player_id:
        raise HTTPException(status_code=403, detail="Only the player who posted a call-out can cancel it.")
    if c.status != "open":
        raise HTTPException(status_code=409, detail="Only an open call-out can be cancelled.")

    c.status = "cancelled"
    c.updated_at = now_uk_naive()
    db.add(c)
    db.commit()
    db.refresh(c)

    # The channel was told the game was going, so it should be told it isn't.
    # Without this a call-out just stopped being mentioned, and anyone who had
    # been meaning to take it up found out by turning up to the app.
    try:
        header = (
            f"🚫 **Call Out withdrawn** — "
            f"{name_with_mention(db, c.creator_name, c.creator_player_id)} "
            f"is no longer looking for this game"
        )
        _post_call_out(db, club_id, c.system, _webhook_content(c, header))
    except Exception:
        pass

    return _serialize(c, my_player_id)
