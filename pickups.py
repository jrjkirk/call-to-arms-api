"""Pick-up games: matchmaking outside club night.

Sam's feature request (2026-10-25), scoped with Joel as steps 1 and 2 together.
It replaces call outs, which asked one player to name an exact time and hoped
someone spotted it at the bottom of the signup page.

A player asks to be found a game: the system, a window they're free in
("Thu 30 Oct, 18:00 to 22:00"), where they'd play, and what they'd bring. The
request joins a pool for that club and system and is posted to Discord. Then
match() pairs compatible requests, by the club night rules wherever they apply:

  - the two windows overlap by at least MIN_OVERLAP;
  - the places agree ("we'll sort it" goes with anything, a named place only
    with the same place, the club only with the club);
  - an exclusive kind of game only meets the same kind (vibes.incompatible);
  - a blocked pair is never matched (club-wide blocks and this system's);
  - among what's left: same kind of game first, then closest points, then
    first come, first served.

Both players are asked to accept within ANSWER_WITHIN (sooner if the game
starts sooner). Both accept: the game is on, posted to Discord, and each is
told who the other is and how to reach them. Anyone who declines, or doesn't
answer in time, leaves the pool and must ask again; the other player goes
back in and is matched again if possible. Why someone declined is PRIVATE: it
is kept for the system's admins (GET /admin/pickups) and shown to nobody else.

Times are UK-local naive datetimes, like call outs. sweep() expires unanswered
proposals and past windows; it runs before every read and from the hourly
scheduler job (scripts/run_pickups_check.py).
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select, or_

import notices
import vibes
from admin import _require_system_scope
from auth import active_club_id, require_user
from call_outs import now_uk_naive
from database import club_app_url, get_session, name_with_mention, scoped
from models import Club, ClubSystem, PairingBlock, PickupMatch, PickupRequest, Player, User
from signups import (
    _effective_vibe_config,
    _effective_vibe_specs_from,
    _get_system_config,
    _post_webhook,
    _require_linked_player,
    _require_system_enabled,
    require_discord_member,
)
from system_overrides import effective_system

router = APIRouter(prefix="/pickups", tags=["pickups"])
admin_router = APIRouter(prefix="/admin/pickups", tags=["pickups"])

MIN_OVERLAP = timedelta(hours=2)
ANSWER_WITHIN = timedelta(hours=24)
MAX_AHEAD = timedelta(days=30)
_MAX_TEXT = 500

OPEN, PROPOSED, MATCHED, DECLINED, CANCELLED, EXPIRED = (
    "open", "proposed", "matched", "declined", "cancelled", "expired")
LIVE = (OPEN, PROPOSED, MATCHED)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _when(start: datetime, end: Optional[datetime] = None) -> str:
    """'Thu 30 Oct, 18:00–22:00', or 'Thu 30 Oct from 18:00'."""
    day = start.strftime("%a %d %b")
    return f"{day}, {start:%H:%M}–{end:%H:%M}" if end else f"{day} from {start:%H:%M}"


def _where(db: Session, r: PickupRequest) -> str:
    # "at the club", not the club's name: a club's full name ran long enough
    # to squeeze the day and time on Home, and every reader is at that club.
    if r.venue == "club":
        return "at the club"
    if r.venue == "other" and r.venue_text:
        return f"at {r.venue_text}"
    return "place to be agreed"


def _overlap(a: PickupRequest, b: PickupRequest) -> Optional[tuple[datetime, datetime]]:
    start, end = max(a.window_start, b.window_start), min(a.window_end, b.window_end)
    return (start, end) if end - start >= MIN_OVERLAP else None


def _venues_agree(a: PickupRequest, b: PickupRequest) -> bool:
    if a.venue == "tbc" or b.venue == "tbc":
        return True
    if a.venue != b.venue:
        return False
    if a.venue == "other":
        return (a.venue_text or "").strip().lower() == (b.venue_text or "").strip().lower()
    return True


def _blocked_pairs(db: Session, club_id: int, system_id: Optional[int]) -> set[frozenset]:
    rows = db.exec(
        scoped(PairingBlock, club_id).where(
            or_(PairingBlock.system_id == None, PairingBlock.system_id == system_id)  # noqa: E711
        )
    ).all()
    return {frozenset((b.player_a_id, b.player_b_id)) for b in rows}


def _vibe_specs(db: Session, club_id: int, system: str):
    config = _get_system_config(db, system)
    if config is None:
        return [], None
    cs = db.exec(select(ClubSystem).where(ClubSystem.club_id == club_id, ClubSystem.system_id == config.id)).first()
    return _effective_vibe_specs_from(cs, config), config.id


def _player_user_id(db: Session, player_id: int) -> Optional[int]:
    p = db.get(Player, player_id)
    return p.user_id if p else None


def _discord_handle(db: Session, player_id: int) -> Optional[str]:
    uid = _player_user_id(db, player_id)
    u = db.get(User, uid) if uid else None
    return u.discord_name if u and u.discord_name else None


def _tell(db: Session, r: PickupRequest, body: str) -> None:
    """A notice (banner + push) to the player behind a request, if they have
    an account. Never breaks the action that caused it."""
    uid = _player_user_id(db, r.player_id)
    if uid is None:
        return
    try:
        notices.record(db, user_id=uid, club_id=r.club_id, system=r.system, week="",
                       kind=notices.PICKUP, body=body, url="/pickups")
    except Exception:
        pass


def _post(db: Session, club_id: int, system: str, content: str) -> None:
    """To the club's call-outs channel (pick-up games inherit it), or its signup
    channel if it hasn't split one out."""
    try:
        _post_webhook(db, club_id, system, content, webhook_type="call_outs", fallback_type="signup")
    except Exception:
        pass


def _link(db: Session, club_id: int) -> str:
    return club_app_url(db.get(Club, club_id), "/pickups")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match(db: Session, club_id: int, system: str) -> list[PickupMatch]:
    """Pair every open request that can be paired, oldest first. Flushed, not
    committed: the caller's transaction owns it."""
    now = now_uk_naive()
    specs, system_id = _vibe_specs(db, club_id, system)
    blocked = _blocked_pairs(db, club_id, system_id)
    pool = db.exec(
        scoped(PickupRequest, club_id)
        .where(PickupRequest.system == system)
        .where(PickupRequest.status == OPEN)
        .where(PickupRequest.window_end > now)
        .order_by(PickupRequest.created_at, PickupRequest.id)
    ).all()

    made: list[PickupMatch] = []
    taken: set[int] = set()
    for r in pool:
        if r.id in taken:
            continue
        best = None
        for o in pool:
            if o.id == r.id or o.id in taken or o.player_id == r.player_id:
                continue
            if frozenset((r.player_id, o.player_id)) in blocked:
                continue
            if vibes.incompatible(r.vibe, o.vibe, specs) or not _venues_agree(r, o):
                continue
            window = _overlap(r, o)
            if window is None or window[1] - max(window[0], now) < MIN_OVERLAP:
                continue
            same_vibe = (r.vibe or "").strip().lower() == (o.vibe or "").strip().lower()
            points_gap = abs((r.points or 0) - (o.points or 0))
            key = (0 if same_vibe else 1, points_gap, o.created_at, o.id)
            if best is None or key < best[0]:
                best = (key, o, window)
        if best is None:
            continue
        _, o, (start, _end) = best
        starts_at = max(start, now)
        # 24 hours to answer, or until half an hour before the game if sooner,
        # but never less than an hour.
        expires_at = max(now + timedelta(hours=1), min(now + ANSWER_WITHIN, starts_at - timedelta(minutes=30)))
        m = PickupMatch(club_id=club_id, system=system, a_request_id=r.id, b_request_id=o.id,
                        starts_at=starts_at, expires_at=expires_at)
        db.add(m)
        r.status = o.status = PROPOSED
        db.add(r)
        db.add(o)
        db.flush()
        taken.update((r.id, o.id))
        made.append(m)
        for me, them in ((r, o), (o, r)):
            _tell(db, me, f"Pick-up game found: you and {them.player_name} for {system}, "
                          f"{_when(starts_at)}, {_where(db, me if me.venue != 'tbc' else them)}. "
                          f"Accept or decline in the app by {expires_at:%a %H:%M}.")
    return made


def _back_in_pool(db: Session, r: PickupRequest, now: datetime) -> None:
    r.status = OPEN if r.window_end > now else EXPIRED
    db.add(r)


def sweep(db: Session, club_id: Optional[int] = None) -> int:
    """Close proposals nobody answered in time (non-answerers leave the pool,
    the other player goes back in), and expire requests whose window has
    passed. Re-matches what went back in. Returns how many rows it changed."""
    now = now_uk_naive()
    changed = 0
    q = select(PickupMatch).where(PickupMatch.status == "proposed").where(PickupMatch.expires_at <= now)
    if club_id is not None:
        q = q.where(PickupMatch.club_id == club_id)
    touched: set[tuple[int, str]] = set()
    for m in db.exec(q).all():
        a, b = db.get(PickupRequest, m.a_request_id), db.get(PickupRequest, m.b_request_id)
        m.status = DECLINED
        m.decided_at = datetime.utcnow()
        m.decline_reason = m.decline_reason or "No answer in time"
        for req, answer in ((a, m.a_answer), (b, m.b_answer)):
            if req is None:
                continue
            if answer == "accepted":
                _back_in_pool(db, req, now)
                if req.status == OPEN:
                    _tell(db, req, "Your pick-up game didn't come together in time, so you're back in the pool.")
            else:
                req.status = DECLINED
                db.add(req)
                if m.declined_by_player_id is None:
                    m.declined_by_player_id = req.player_id
                _tell(db, req, "A pick-up game was found for you but wasn't answered in time, "
                               "so your request has closed. Ask again whenever you like.")
        db.add(m)
        touched.add((m.club_id, m.system))
        changed += 1

    q = select(PickupRequest).where(PickupRequest.status == OPEN).where(PickupRequest.window_end <= now)
    if club_id is not None:
        q = q.where(PickupRequest.club_id == club_id)
    for r in db.exec(q).all():
        r.status = EXPIRED
        db.add(r)
        changed += 1

    for cid, system in touched:
        match(db, cid, system)
    return changed


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _request_out(db: Session, r: PickupRequest) -> dict:
    return {
        "id": r.id, "system": r.system, "player_id": r.player_id, "player_name": r.player_name,
        "window_start": r.window_start.isoformat(), "window_end": r.window_end.isoformat(),
        "when": _when(r.window_start, r.window_end), "venue": r.venue, "venue_text": r.venue_text,
        "where": _where(db, r), "faction": r.faction, "points": r.points, "vibe": r.vibe,
        "notes": r.notes, "status": r.status,
    }


def _match_for(db: Session, r: PickupRequest) -> Optional[PickupMatch]:
    return db.exec(
        select(PickupMatch)
        .where(or_(PickupMatch.a_request_id == r.id, PickupMatch.b_request_id == r.id))
        .where(PickupMatch.status.in_(("proposed", "confirmed")))
        .order_by(PickupMatch.id.desc())
    ).first()


def _match_out(db: Session, m: PickupMatch, mine: PickupRequest) -> dict:
    other_id = m.b_request_id if m.a_request_id == mine.id else m.a_request_id
    other = db.get(PickupRequest, other_id)
    my_answer = m.a_answer if m.a_request_id == mine.id else m.b_answer
    their_answer = m.b_answer if m.a_request_id == mine.id else m.a_answer
    out = {
        "id": m.id, "status": m.status, "starts_at": m.starts_at.isoformat(), "when": _when(m.starts_at),
        "expires_at": m.expires_at.isoformat(), "my_answer": my_answer, "their_answer": their_answer,
        "where": _where(db, mine if mine.venue != "tbc" else other) if other else _where(db, mine),
        "opponent": None,
    }
    if other:
        out["opponent"] = {
            "player_id": other.player_id, "name": other.player_name, "faction": other.faction,
            "points": other.points, "vibe": other.vibe, "notes": other.notes,
            # How to reach them, once the game is on.
            "discord": _discord_handle(db, other.player_id) if m.status == "confirmed" else None,
        }
    return out


@router.get("")
def list_pickups(
    system: Optional[str] = None,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Your requests (with any proposed or confirmed game), and who else is
    looking. Optionally one system."""
    if sweep(db, club_id):
        db.commit()
    player = _require_linked_player(user, db, club_id)
    now = now_uk_naive()
    q = scoped(PickupRequest, club_id).where(PickupRequest.status.in_(LIVE))
    if system:
        q = q.where(PickupRequest.system == system)
    rows = db.exec(q.order_by(PickupRequest.window_start)).all()
    mine, others = [], []
    for r in rows:
        if r.player_id == player.id:
            item = _request_out(db, r)
            m = _match_for(db, r)
            item["match"] = _match_out(db, m, r) if m else None
            mine.append(item)
        elif r.status == OPEN and r.window_end > now:
            others.append(_request_out(db, r))
    return {"mine": mine, "others": others}


# ---------------------------------------------------------------------------
# Asking, answering, withdrawing
# ---------------------------------------------------------------------------

class PickupIn(BaseModel):
    system: str
    date: str               # YYYY-MM-DD, UK local
    start: str              # HH:MM
    end: str                # HH:MM, same day
    venue: str = "club"     # club | tbc | other
    venue_text: Optional[str] = None
    faction: Optional[str] = None
    points: Optional[int] = None
    vibe: Optional[str] = None
    notes: Optional[str] = None


@router.post("")
def create_pickup(
    body: PickupIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    config = effective_system(db, club_id, config)
    _require_system_enabled(db, club_id, body.system)
    player = _require_linked_player(user, db, club_id)
    require_discord_member(db, player, club_id, body.system)

    try:
        start = datetime.strptime(f"{body.date} {body.start}", "%Y-%m-%d %H:%M")
        end = datetime.strptime(f"{body.date} {body.end}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=422, detail="Please give a valid date and times.")
    now = now_uk_naive()
    if end <= start:
        raise HTTPException(status_code=422, detail="The end time must be after the start time.")
    if end - max(start, now) < MIN_OVERLAP:
        raise HTTPException(status_code=422, detail="Give at least two hours, still to come, for a game.")
    if start > now + MAX_AHEAD:
        raise HTTPException(status_code=422, detail="Pick-up games can be up to 30 days ahead.")
    if body.venue not in ("club", "tbc", "other"):
        raise HTTPException(status_code=422, detail="Choose where you'd play.")
    venue_text = (body.venue_text or "").strip()[:120] or None
    if body.venue == "other" and not venue_text:
        raise HTTPException(status_code=422, detail="Say where you'd like to play.")

    # One live request per player per system: a second would let the matcher
    # offer them two games at once.
    live = db.exec(
        scoped(PickupRequest, club_id).where(PickupRequest.system == body.system)
        .where(PickupRequest.player_id == player.id).where(PickupRequest.status.in_(LIVE))
    ).first()
    if live is not None:
        raise HTTPException(status_code=409, detail="You already have a pick-up request for this system. Cancel it to ask again.")

    vibe_options, default_vibe = _effective_vibe_config(db, club_id, config)
    vibe = body.vibe if body.vibe in (vibe_options or []) else default_vibe
    points = max(0, min(int(body.points or config.default_points), config.max_points)) if config.uses_points else None

    r = PickupRequest(
        club_id=club_id, system=body.system, player_id=player.id, player_name=player.name,
        window_start=start, window_end=end, venue=body.venue, venue_text=venue_text if body.venue == "other" else None,
        faction=(body.faction or "").strip() or None, points=points, vibe=vibe,
        notes=((body.notes or "").strip() or None) and body.notes.strip()[:_MAX_TEXT],
    )
    db.add(r)
    db.flush()
    _post(db, club_id, body.system,
          f"📣 {name_with_mention(db, player.name, player.id)} is looking for a pick-up game of {body.system}: "
          f"{_when(start, end)}, {_where(db, r)}"
          + (f" · {vibe}" if vibe else "") + (f" · {points} pts" if points else "")
          + f"\nFind a game too: {_link(db, club_id)}")
    match(db, club_id, body.system)
    db.commit()
    db.refresh(r)
    out = _request_out(db, r)
    m = _match_for(db, r)
    out["match"] = _match_out(db, m, r) if m else None
    return out


def _my_request(db: Session, request_id: int, club_id: int, player: Player) -> PickupRequest:
    r = db.get(PickupRequest, request_id)
    if r is None or r.club_id != club_id or r.player_id != player.id:
        raise HTTPException(status_code=404, detail="Pick-up request not found.")
    return r


def _my_side(db: Session, match_id: int, club_id: int, player: Player) -> tuple[PickupMatch, PickupRequest, PickupRequest, str]:
    m = db.get(PickupMatch, match_id)
    if m is None or m.club_id != club_id:
        raise HTTPException(status_code=404, detail="Pick-up game not found.")
    a, b = db.get(PickupRequest, m.a_request_id), db.get(PickupRequest, m.b_request_id)
    if a and a.player_id == player.id:
        return m, a, b, "a"
    if b and b.player_id == player.id:
        return m, b, a, "b"
    raise HTTPException(status_code=404, detail="Pick-up game not found.")


@router.post("/matches/{match_id}/accept")
def accept(
    match_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    sweep(db, club_id)
    player = _require_linked_player(user, db, club_id)
    m, mine, other, side = _my_side(db, match_id, club_id, player)
    if m.status != "proposed":
        raise HTTPException(status_code=409, detail="This game is no longer waiting for an answer.")
    setattr(m, f"{side}_answer", "accepted")
    if m.a_answer == m.b_answer == "accepted":
        m.status = "confirmed"
        m.decided_at = datetime.utcnow()
        mine.status = other.status = MATCHED
        db.add(other)
        where = _where(db, mine if mine.venue != "tbc" else other)
        _post(db, club_id, m.system,
              f"✅ Pick-up game on: {name_with_mention(db, mine.player_name, mine.player_id)} vs "
              f"{name_with_mention(db, other.player_name, other.player_id)}, {m.system}, {_when(m.starts_at)}, {where}.")
        for me, them in ((mine, other), (other, mine)):
            handle = _discord_handle(db, them.player_id)
            _tell(db, me, f"Your pick-up game is on: you vs {them.player_name}, {_when(m.starts_at)}, {where}. "
                          + (f"Message them on Discord ({handle}) to sort the details." if handle
                             else "Sort the details between you."))
    db.add(m)
    db.add(mine)
    db.commit()
    return _match_out(db, m, mine)


class DeclineIn(BaseModel):
    reason: Optional[str] = None


@router.post("/matches/{match_id}/decline")
def decline(
    match_id: int,
    body: DeclineIn,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Turn a proposed game down. You leave the pool (ask again to come back);
    the other player goes back in, and is told only that it fell through. The
    reason is for the system's admins only."""
    sweep(db, club_id)
    player = _require_linked_player(user, db, club_id)
    m, mine, other, side = _my_side(db, match_id, club_id, player)
    if m.status != "proposed":
        raise HTTPException(status_code=409, detail="This game is no longer waiting for an answer.")
    now = now_uk_naive()
    setattr(m, f"{side}_answer", "declined")
    m.status = DECLINED
    m.decided_at = datetime.utcnow()
    m.declined_by_player_id = player.id
    m.decline_reason = ((body.reason or "").strip()[:_MAX_TEXT]) or "No reason given"
    mine.status = DECLINED
    _back_in_pool(db, other, now)
    if other.status == OPEN:
        _tell(db, other, "Your proposed pick-up game fell through, so you're back in the pool.")
    db.add(m)
    db.add(mine)
    db.flush()
    match(db, club_id, m.system)
    db.commit()
    return {"declined": True}


@router.post("/{request_id}/cancel")
def cancel(
    request_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Withdraw a request. If it was waiting on a proposal, that counts as
    declining it; if a game was on, the other player is told it's off and goes
    back in the pool."""
    sweep(db, club_id)
    player = _require_linked_player(user, db, club_id)
    r = _my_request(db, request_id, club_id, player)
    if r.status not in LIVE:
        return {"cancelled": True}
    now = now_uk_naive()
    m = _match_for(db, r)
    if m is not None:
        other = db.get(PickupRequest, m.b_request_id if m.a_request_id == r.id else m.a_request_id)
        was_on = m.status == "confirmed"
        m.status = "cancelled" if was_on else DECLINED
        m.decided_at = datetime.utcnow()
        m.declined_by_player_id = player.id
        m.decline_reason = m.decline_reason or ("Called off after confirming" if was_on else "Withdrew the request")
        db.add(m)
        if other is not None:
            _back_in_pool(db, other, now)
            if other.status == OPEN:
                _tell(db, other, f"{r.player_name} can't make your pick-up game any more, so you're back in the pool."
                      if was_on else "Your proposed pick-up game fell through, so you're back in the pool.")
            if was_on:
                _post(db, club_id, r.system, f"❌ Pick-up game off: {r.player_name} vs {other.player_name}, {_when(m.starts_at)}.")
    r.status = CANCELLED
    db.add(r)
    db.flush()
    match(db, club_id, r.system)
    db.commit()
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Admin: the private record of declines
# ---------------------------------------------------------------------------

@admin_router.get("")
def admin_pickups(
    system: str,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Recent pick-up matches for one system, with why any were declined.
    Admins of that system only: reasons are never shown to players."""
    _require_system_scope(system, user, db)
    rows = db.exec(
        scoped(PickupMatch, club_id).where(PickupMatch.system == system)
        .order_by(PickupMatch.id.desc()).limit(100)
    ).all()
    out = []
    for m in rows:
        a, b = db.get(PickupRequest, m.a_request_id), db.get(PickupRequest, m.b_request_id)
        decliner = None
        if m.declined_by_player_id is not None:
            decliner = next((x.player_name for x in (a, b) if x and x.player_id == m.declined_by_player_id), None)
        out.append({
            "id": m.id, "status": m.status, "when": _when(m.starts_at),
            "players": [x.player_name for x in (a, b) if x],
            "declined_by": decliner, "reason": m.decline_reason,
            "proposed_at": m.proposed_at.isoformat(),
        })
    return out
