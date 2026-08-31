"""Tournament endpoints: the TO console, plus the read side players will use.

Phase 1 of the events feature. What it deliberately does NOT do yet: post to
Discord, take entry fees, or validate army lists. See the scope doc.

Authorisation follows the rest of the app: running a tournament for a system
means holding that system's admin scope at the active club, so the person who
already runs Kill Team nights is the person who can run a Kill Team event.
"""
import random
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

import tournament_pairing as tp
import tournament_schedule as sched
import tournament_scoring as scoring
from auth import active_club_id, admin_scopes, current_user, public_club_id, require_user
from database import active_player_id_for, get_session
from models import (
    Player, SystemConfig, Tournament, TournamentEntry, TournamentGame,
    TournamentRound, User, VenueTable,
)

router = APIRouter(prefix="/tournaments", tags=["tournaments"])

# Tournaments are still being built out. Until that finishes, every route here
# is platform-admin only — hiding the nav tab alone would leave the endpoints
# open to anyone who guessed the URL, which is not the same thing at all.
# Removing this is the switch that makes the feature live.
PLATFORM_ADMIN_ONLY = True


def _gate(user: Optional[User]) -> None:
    if not PLATFORM_ADMIN_ONLY:
        return
    if user is None or not user.is_platform_admin:
        raise HTTPException(status_code=404, detail="Not found.")

# Statuses a player-visible tournament can be in. `draft` is the TO's private
# workspace and never appears on a public list.
PUBLIC_STATUSES = ("open", "closed", "running", "finished")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(db: Session, club_id: int, tournament_id: int) -> Tournament:
    t = db.get(Tournament, tournament_id)
    if t is None or t.club_id != club_id:
        raise HTTPException(status_code=404, detail="Tournament not found.")
    return t


def _require_to(db: Session, user: User, club_id: int, t: Tournament) -> None:
    """Running an event for a system means administering that system here."""
    system = db.get(SystemConfig, t.system_id)
    scopes = admin_scopes(user, db, club_id)
    if system is not None and system.name in scopes:
        return
    if system is not None and (system.legacy_system_name or "") in scopes:
        return
    raise HTTPException(
        status_code=403,
        detail="You need admin access for this game system to run its events.",
    )


def _entries(db: Session, tournament_id: int) -> list[TournamentEntry]:
    return db.exec(
        select(TournamentEntry)
        .where(TournamentEntry.tournament_id == tournament_id)
        .order_by(TournamentEntry.id)
    ).all()


def _games(db: Session, tournament_id: int) -> list[TournamentGame]:
    return db.exec(
        select(TournamentGame)
        .where(TournamentGame.tournament_id == tournament_id)
        .order_by(TournamentGame.id)
    ).all()


def _rounds(db: Session, tournament_id: int) -> list[TournamentRound]:
    return db.exec(
        select(TournamentRound)
        .where(TournamentRound.tournament_id == tournament_id)
        .order_by(TournamentRound.round_no)
    ).all()


def _entry_dict(e: TournamentEntry) -> dict:
    return {
        "id": e.id, "name": e.display_name, "player_id": e.player_id,
        "faction": e.faction, "status": e.status, "seed": e.seed,
        "bracket": e.bracket, "painting": e.painting_score,
        "ticket_status": e.ticket_status,
        "list_submitted": e.list_submitted_at is not None,
        "army_list": e.army_list, "notes": e.notes,
    }


def _game_dict(db: Session, g: TournamentGame, names: dict) -> dict:
    table = db.get(VenueTable, g.table_id) if g.table_id else None
    return {
        "id": g.id, "round_id": g.round_id,
        "a": {"entry_id": g.a_entry_id, "name": names.get(g.a_entry_id, "?"),
              "score": g.a_score, "sports": g.a_sports},
        "b": ({"entry_id": g.b_entry_id, "name": names.get(g.b_entry_id, "?"),
               "score": g.b_score, "sports": g.b_sports}
              if g.b_entry_id else None),
        "table": table.name if table else g.table_label,
        "table_id": g.table_id,
        "result": g.result,
        "confirmed": g.confirmed,
    }


def _summary(db: Session, t: Tournament, entries=None) -> dict:
    system = db.get(SystemConfig, t.system_id)
    entries = entries if entries is not None else _entries(db, t.id)
    counted = [e for e in entries if e.status != "dropped"]
    return {
        "id": t.id, "name": t.name, "blurb": t.blurb,
        "date": t.event_date.isoformat(), "start_time": t.start_time,
        "system": system.name if system else None,
        "system_id": t.system_id,
        "system_slug": system.slug if system else None,
        # NULL means "use the game system's logo", resolved client-side from
        # /logos/<slug>.png — so every event has a picture with no work.
        "image_url": t.image_url,
        "ticket_price_pence": t.ticket_price_pence,
        "ticket_url": t.ticket_url,
        "list_required": t.list_required,
        "list_deadline": t.list_deadline.isoformat() if t.list_deadline else None,
        "rounds": t.rounds, "points_limit": t.points_limit,
        "capacity": t.capacity, "status": t.status,
        "entries": len(counted),
        "checked_in": len([e for e in entries if e.status == "checked_in"]),
        "waitlisted": len([e for e in entries if e.status == "waitlisted"]),
        "win_points": t.win_points, "draw_points": t.draw_points,
        "loss_points": t.loss_points, "bye_points": t.bye_points,
        "seeding": t.seeding, "brackets": t.brackets or [],
        "end_date": t.end_date.isoformat() if t.end_date else None,
        "days": t.days, "round_minutes": t.round_minutes,
        "day_dates": sched.day_dates(t),
        # Never empty: a first-time TO gets a workable running order rather
        # than a blank grid, and can then change any of it.
        "schedule": t.schedule or sched.generate(t),
        "schedule_is_default": not t.schedule,
        "scoring": scoring.config(t),
        # The formula in plain sentences, for printing next to the standings.
        # An unpublished sort key is itself a problem — players can't check a
        # result against a rule they were never shown.
        "scoring_explained": scoring.describe(t),
        "scoring_options": {
            "primary": list(scoring.PRIMARY),
            "tiebreakers": list(scoring.TIEBREAKERS),
            "vp_mode": ["raw", "capped", "normalised"],
            "bye_vp_mode": ["fixed", "field_average", "own_average"],
            "sports_mode": ["tiebreak", "multiplier", "bonus"],
            "painting_mode": ["tiebreak", "bonus"],
        },
    }


def _standings(db: Session, t: Tournament, for_admin: bool = False) -> list[dict]:
    """Standings. Sportsmanship and painting are TO-only.

    Both are judgements about a person rather than a record of what happened at
    the table, and showing a player the sportsmanship number their opponents
    gave them mid-event is a good way to sour an afternoon. They still COUNT
    where the event is configured to use them; they are simply not itemised
    back to the field.
    """
    entries = _entries(db, t.id)
    rows = scoring.compute(t, entries, _games(db, t.id))
    return [{
        "rank": i + 1, "entry_id": s.entry_id, "name": s.name,
        "bracket": s.bracket,
        "points": s.points, "win_points": s.win_points, "played": s.played,
        "wins": s.wins, "draws": s.draws, "losses": s.losses, "byes": s.byes,
        "vp_for": round(s.vp_for, 2), "raw_vp": s.raw_vp,
        "diff": round(s.diff, 2), "sos": s.sos,
        **({"sports": s.sports_avg, "painting": s.painting} if for_admin else {}),
        "dropped": s.dropped,
    } for i, s in enumerate(rows)]


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------

@router.get("")
def list_tournaments(
    include_drafts: bool = Query(False),
    user: Optional[User] = Depends(current_user),
    club_id: int = Depends(public_club_id),
    db: Session = Depends(get_session),
):
    """This club's events. Drafts are TO-only and need an admin asking."""
    _gate(user)
    q = select(Tournament).where(Tournament.club_id == club_id)
    if not (include_drafts and user and admin_scopes(user, db, club_id)):
        q = q.where(Tournament.status.in_(PUBLIC_STATUSES))
    rows = db.exec(q.order_by(Tournament.event_date.desc())).all()
    return {"tournaments": [_summary(db, t) for t in rows]}


@router.get("/{tournament_id}")
def get_tournament(
    tournament_id: int,
    user: Optional[User] = Depends(current_user),
    club_id: int = Depends(public_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    if t.status == "draft" and not (user and admin_scopes(user, db, club_id)):
        raise HTTPException(status_code=404, detail="Tournament not found.")

    entries = _entries(db, t.id)
    names = {e.id: e.display_name for e in entries}
    rounds = _rounds(db, t.id)
    games = _games(db, t.id)
    is_admin = bool(user and admin_scopes(user, db, club_id))

    # A round that isn't published yet is the TO's business only — players
    # seeing pairings before the TO has checked them is exactly the mistake the
    # weekly pairings publish step exists to prevent.
    visible = [r for r in rounds if is_admin or r.status != "paired"]

    my_entry_id = None
    if user:
        pid = active_player_id_for(db, user, club_id)
        mine = next((e for e in entries
                     if (pid and e.player_id == pid) or e.user_id == user.id), None)
        my_entry_id = mine.id if mine else None

    return {
        **_summary(db, t, entries),
        "is_admin": is_admin,
        "my_entry_id": my_entry_id,
        "entries": [_entry_dict(e) for e in entries],
        # `rounds` from _summary is the configured COUNT; the list lives under
        # its own key so spreading one into the other can't silently replace a
        # number with an array.
        "rounds_total": t.rounds,
        "rounds": [{
            "id": r.id, "round_no": r.round_no, "status": r.status,
            "slot": sched.round_slot(t, r.round_no),
            "games": [_game_dict(db, g, names) for g in games if g.round_id == r.id],
        } for r in visible],
        "standings": _standings(db, t, for_admin=is_admin),
        # Everything the player view needs to render itself, so it doesn't have
        # to reason about entries it isn't allowed to see.
        "me": _me(db, t, entries, games, my_entry_id),
    }


def _me(db: Session, t: Tournament, entries, games, my_entry_id) -> Optional[dict]:
    """This player's own slice of the event: their entry, and their games."""
    if not my_entry_id:
        return None
    entry = next((e for e in entries if e.id == my_entry_id), None)
    if entry is None:
        return None
    names = {e.id: e.display_name for e in entries}
    rounds = {r.id: r for r in _rounds(db, t.id)}
    mine = []
    for g in games:
        if my_entry_id not in (g.a_entry_id, g.b_entry_id):
            continue
        r = rounds.get(g.round_id)
        # A player must not see a round the TO hasn't published, the same rule
        # the weekly pairings follow.
        if r is None or r.status == "paired":
            continue
        i_am_a = g.a_entry_id == my_entry_id
        opp_id = g.b_entry_id if i_am_a else g.a_entry_id
        mine.append({
            "game_id": g.id, "round_no": r.round_no,
            "opponent": names.get(opp_id) if opp_id else None,
            "table": g.table_label,
            "my_score": g.a_score if i_am_a else g.b_score,
            "their_score": g.b_score if i_am_a else g.a_score,
            "result": ("bye" if g.result == "bye" else
                       None if not g.result else
                       "win" if (g.result == "a") == i_am_a else
                       "draw" if g.result == "draw" else "loss"),
            "confirmed": g.confirmed,
            "i_am_a": i_am_a,
        })
    return {
        "entry_id": entry.id, "name": entry.display_name,
        "status": entry.status, "faction": entry.faction,
        "ticket_status": entry.ticket_status,
        "army_list": entry.army_list,
        "list_submitted": entry.list_submitted_at is not None,
        "games": mine,
    }


# ---------------------------------------------------------------------------
# TO console — create and configure
# ---------------------------------------------------------------------------

class TournamentBody(BaseModel):
    system_id: int
    name: str
    event_date: str
    start_time: Optional[str] = None
    blurb: Optional[str] = None
    rounds: int = 3
    days: int = 1
    round_minutes: int = 150
    points_limit: Optional[int] = None
    capacity: Optional[int] = None


@router.post("")
def create_tournament(
    body: TournamentBody,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    try:
        when = date.fromisoformat(body.event_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD.")
    if not (1 <= body.rounds <= 12):
        raise HTTPException(status_code=422, detail="Rounds must be between 1 and 12.")
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Give the event a name.")

    t = Tournament(
        club_id=club_id, system_id=body.system_id, name=body.name.strip(),
        blurb=(body.blurb or "").strip() or None,
        event_date=when, start_time=body.start_time,
        rounds=body.rounds, days=max(1, body.days),
        round_minutes=max(30, body.round_minutes),
        end_date=(when + timedelta(days=body.days - 1)) if body.days > 1 else None,
        points_limit=body.points_limit,
        capacity=body.capacity, created_by_user_id=user.id,
    )
    _require_to(db, user, club_id, t)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _summary(db, t)


class TournamentPatch(BaseModel):
    name: Optional[str] = None
    blurb: Optional[str] = None
    event_date: Optional[str] = None
    start_time: Optional[str] = None
    rounds: Optional[int] = None
    points_limit: Optional[int] = None
    capacity: Optional[int] = None
    status: Optional[str] = None
    seeding: Optional[str] = None
    brackets: Optional[list] = None
    days: Optional[int] = None
    round_minutes: Optional[int] = None
    image_url: Optional[str] = None
    ticket_price_pence: Optional[int] = None
    ticket_url: Optional[str] = None
    list_required: Optional[bool] = None
    schedule: Optional[list] = None
    regenerate_schedule: Optional[bool] = None
    scoring: Optional[dict] = None
    win_points: Optional[int] = None
    draw_points: Optional[int] = None
    loss_points: Optional[int] = None
    bye_points: Optional[int] = None
    tiebreakers: Optional[list] = None


@router.patch("/{tournament_id}")
def patch_tournament(
    tournament_id: int,
    body: TournamentPatch,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)

    if body.status is not None:
        if body.status not in ("draft", "open", "closed", "running", "finished"):
            raise HTTPException(status_code=422, detail="Unknown status.")
        t.status = body.status
    if body.event_date is not None:
        try:
            t.event_date = date.fromisoformat(body.event_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD.")
    if body.days is not None:
        if not (1 <= body.days <= 7):
            raise HTTPException(status_code=422, detail="An event can run over 1 to 7 days.")
        t.days = body.days
    if body.round_minutes is not None:
        if not (30 <= body.round_minutes <= 480):
            raise HTTPException(status_code=422, detail="A round is 30 to 480 minutes long.")
        t.round_minutes = body.round_minutes
    if body.schedule is not None:
        t.schedule = sched.normalise(body.schedule, t)
    if body.scoring is not None:
        errs = scoring.validate(body.scoring)
        if errs:
            raise HTTPException(status_code=422, detail=" ".join(errs))
        # Merged, not replaced, so a partial save can't silently reset knobs
        # the caller didn't mention.
        t.scoring = {**(t.scoring or {}), **body.scoring}
    if body.seeding is not None:
        if body.seeding not in ("random", "seeded"):
            raise HTTPException(status_code=422, detail="Seeding must be random or seeded.")
        t.seeding = body.seeding
    if body.brackets is not None:
        t.brackets = [b.strip() for b in body.brackets if str(b).strip()] or None
    if body.tiebreakers is not None:
        bad = [x for x in body.tiebreakers if x not in scoring.TIEBREAKERS]
        if bad:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown tiebreaker(s): {', '.join(bad)}. "
                       f"Pick from {', '.join(scoring.TIEBREAKERS)}.",
            )
        t.tiebreakers = body.tiebreakers

    for f in ("name", "blurb", "start_time", "rounds", "points_limit", "capacity",
              "image_url", "ticket_price_pence", "ticket_url", "list_required",
              "win_points", "draw_points", "loss_points", "bye_points"):
        v = getattr(body, f)
        if v is not None:
            setattr(t, f, v.strip() if isinstance(v, str) else v)

    # end_date is derived, never typed — two fields that must agree are two
    # fields that eventually won't.
    t.end_date = (t.event_date + timedelta(days=t.days - 1)) if t.days > 1 else None

    # Changing the shape of the event invalidates a schedule built for the old
    # shape, so a stored one is regenerated rather than left describing rounds
    # that no longer exist.
    if body.regenerate_schedule or (
        t.schedule and (body.days is not None or body.rounds is not None
                        or body.round_minutes is not None)
    ):
        t.schedule = sched.generate(t)

    t.updated_at = datetime.utcnow()
    db.add(t)
    db.commit()
    db.refresh(t)
    return _summary(db, t)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

class EntryBody(BaseModel):
    display_name: Optional[str] = None
    contact_email: Optional[str] = None
    faction: Optional[str] = None
    army_list: Optional[str] = None
    notes: Optional[str] = None
    player_id: Optional[int] = None


@router.post("/{tournament_id}/entries")
def add_entry(
    tournament_id: int,
    body: EntryBody,
    user: Optional[User] = Depends(current_user),
    club_id: int = Depends(public_club_id),
    db: Session = Depends(get_session),
):
    """Register. Serves both a player entering themselves and a TO adding
    someone at the door, which is why it is optional-auth: an open event is the
    whole point of the network, and a visitor from another club may have no
    Player row here at all."""
    _gate(user)
    t = _get(db, club_id, tournament_id)
    is_admin = bool(user and admin_scopes(user, db, club_id))

    if t.status not in ("open",) and not is_admin:
        raise HTTPException(status_code=409, detail="This event isn't taking entries.")
    if t.status in ("draft",) and not is_admin:
        raise HTTPException(status_code=404, detail="Tournament not found.")

    entries = _entries(db, t.id)

    # An admin who supplies a name is adding SOMEBODY ELSE — a walk-in, or a
    # visitor from another club. Their own identity must not be stamped onto
    # that entry, which is what happened before: every player a TO added at the
    # door came out owned by the TO's player record, so the TO then matched as
    # "entered" and could report those games as their own.
    adding_someone_else = is_admin and bool((body.display_name or "").strip())
    player_id = body.player_id if is_admin else None
    if user and player_id is None and not adding_someone_else:
        player_id = active_player_id_for(db, user, club_id)
    owner_user_id = user.id if (user and not adding_someone_else) else None

    if owner_user_id:
        already = next((e for e in entries if e.user_id == owner_user_id), None)
        if already:
            raise HTTPException(status_code=409, detail="You're already entered.")

    name = (body.display_name or "").strip()
    if not name and player_id:
        p = db.get(Player, player_id)
        name = p.name if p else ""
    if not name and user:
        name = user.discord_name or ""
    if not name:
        raise HTTPException(status_code=422, detail="Please give a name.")

    # Over capacity goes to the waitlist rather than being refused — a drop-out
    # is common and a waitlist is what turns one into a filled place.
    live = [e for e in entries if e.status in ("registered", "checked_in")]
    status = "registered"
    if t.capacity and len(live) >= t.capacity:
        status = "waitlisted"

    e = TournamentEntry(
        tournament_id=t.id, player_id=player_id,
        user_id=owner_user_id,
        display_name=name,
        contact_email=(body.contact_email or "").strip() or None,
        faction=(body.faction or "").strip() or None,
        army_list=(body.army_list or "").strip() or None,
        notes=(body.notes or "").strip() or None,
        status=status,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"ok": True, "entry": _entry_dict(e), "waitlisted": status == "waitlisted"}


class EntryPatch(BaseModel):
    status: Optional[str] = None
    ticket_status: Optional[str] = None
    faction: Optional[str] = None
    display_name: Optional[str] = None
    seed: Optional[int] = None
    bracket: Optional[str] = None
    painting_score: Optional[int] = None
    army_list: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/{tournament_id}/entries/{entry_id}")
def patch_entry(
    tournament_id: int,
    entry_id: int,
    body: EntryPatch,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)
    e = db.get(TournamentEntry, entry_id)
    if e is None or e.tournament_id != t.id:
        raise HTTPException(status_code=404, detail="Entry not found.")

    if body.status is not None:
        if body.status not in ("registered", "waitlisted", "checked_in", "dropped"):
            raise HTTPException(status_code=422, detail="Unknown entry status.")
        # Checking someone in who hasn't paid is allowed but not silent — the
        # TO is told, and decides. Blocking it outright would be wrong: people
        # pay at the door, and a hard stop at 9am is the last thing a TO needs.
        e.status = body.status
    if body.ticket_status is not None:
        if body.ticket_status not in ("none", "paid", "comp", "refunded"):
            raise HTTPException(status_code=422, detail="Unknown ticket status.")
        e.ticket_status = body.ticket_status
    for f in ("faction", "display_name", "seed", "bracket", "painting_score",
              "army_list", "notes"):
        v = getattr(body, f)
        if v is not None:
            setattr(e, f, v.strip() if isinstance(v, str) else v)

    e.updated_at = datetime.utcnow()
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"ok": True, "entry": _entry_dict(e)}


@router.delete("/{tournament_id}/entries/{entry_id}")
def delete_entry(
    tournament_id: int,
    entry_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)
    e = db.get(TournamentEntry, entry_id)
    if e is None or e.tournament_id != t.id:
        raise HTTPException(status_code=404, detail="Entry not found.")

    # Deleting someone who has played would leave their opponents' records
    # referring to a person who no longer exists. Drop them instead — the games
    # stand, which is what a TO means by "they left".
    if db.exec(select(TournamentGame).where(
        (TournamentGame.a_entry_id == entry_id) | (TournamentGame.b_entry_id == entry_id)
    )).first():
        e.status = "dropped"
        db.add(e)
        db.commit()
        return {"ok": True, "dropped_instead": True}

    db.delete(e)
    db.commit()
    return {"ok": True}


@router.post("/{tournament_id}/check-in-all")
def check_in_all(
    tournament_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Check everyone registered in at once, for the TO who has a room in front
    of them and a round to start."""
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)
    n = 0
    for e in _entries(db, t.id):
        if e.status == "registered":
            e.status = "checked_in"
            db.add(e)
            n += 1
    db.commit()
    return {"ok": True, "checked_in": n}


# ---------------------------------------------------------------------------
# Rounds
# ---------------------------------------------------------------------------

@router.post("/{tournament_id}/rounds")
def generate_round(
    tournament_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Pair the next round. Refuses if the previous one still has open games —
    Swiss pairs on records, so pairing round three while round two is half
    unscored produces a table that is simply wrong."""
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)

    rounds = _rounds(db, t.id)
    if len(rounds) >= t.rounds:
        raise HTTPException(
            status_code=409,
            detail=f"All {t.rounds} rounds have been generated. Raise the round count to add more.",
        )
    games = _games(db, t.id)
    if rounds:
        last = rounds[-1]
        unscored = [g for g in games if g.round_id == last.id and not g.result]
        if unscored:
            raise HTTPException(
                status_code=409,
                detail=f"Round {last.round_no} still has {len(unscored)} game(s) without a result.",
            )

    entries = _entries(db, t.id)
    if not [e for e in entries if e.status == "checked_in"]:
        raise HTTPException(
            status_code=409,
            detail="Nobody is checked in yet, so there's nobody to pair.",
        )

    round_no = len(rounds) + 1
    pairs = tp.pair_round(t, entries, games, round_no, rng=random.Random())
    if not pairs:
        raise HTTPException(status_code=409, detail="Not enough checked-in players to pair.")

    r = TournamentRound(tournament_id=t.id, round_no=round_no, status="paired")
    db.add(r)
    db.flush()

    for i, p in enumerate(pairs, start=1):
        db.add(TournamentGame(
            round_id=r.id, tournament_id=t.id,
            a_entry_id=p.a_entry_id, b_entry_id=p.b_entry_id,
            table_label=str(i) if p.b_entry_id else None,
            # A bye is a completed game the moment it is made — nobody plays it.
            result="bye" if p.b_entry_id is None else None,
            confirmed=p.b_entry_id is None,
        ))

    if t.status in ("open", "closed"):
        t.status = "running"
    t.updated_at = datetime.utcnow()
    db.add(t)
    db.commit()
    db.refresh(r)

    names = {e.id: e.display_name for e in entries}
    return {
        "ok": True,
        "round": {"id": r.id, "round_no": r.round_no, "status": r.status,
                  "games": [_game_dict(db, g, names) for g in _games(db, t.id)
                            if g.round_id == r.id]},
    }


@router.post("/{tournament_id}/rounds/{round_id}/publish")
def publish_round(
    tournament_id: int,
    round_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)
    r = db.get(TournamentRound, round_id)
    if r is None or r.tournament_id != t.id:
        raise HTTPException(status_code=404, detail="Round not found.")
    r.status = "published"
    r.updated_at = datetime.utcnow()
    db.add(r)
    db.commit()
    return {"ok": True}


@router.delete("/{tournament_id}/rounds/{round_id}")
def delete_round(
    tournament_id: int,
    round_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Undo a round the TO isn't happy with. Only ever the LAST one, and only
    while nothing has been scored — deleting an earlier round would invalidate
    every pairing made after it."""
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)
    rounds = _rounds(db, t.id)
    if not rounds or rounds[-1].id != round_id:
        raise HTTPException(status_code=409, detail="Only the latest round can be removed.")

    games = [g for g in _games(db, t.id) if g.round_id == round_id]
    if any(g.result and g.result != "bye" for g in games):
        raise HTTPException(
            status_code=409,
            detail="This round already has results. Clear them first if you really mean to re-pair.",
        )
    for g in games:
        db.delete(g)
    db.delete(rounds[-1])
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

class ResultBody(BaseModel):
    result: Optional[str] = None          # "a" | "b" | "draw" | null to clear
    a_score: Optional[int] = None
    b_score: Optional[int] = None
    a_sports: Optional[int] = None        # what A was given by B
    b_sports: Optional[int] = None
    table_label: Optional[str] = None


@router.patch("/{tournament_id}/games/{game_id}")
def set_result(
    tournament_id: int,
    game_id: int,
    body: ResultBody,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    _require_to(db, user, club_id, t)
    g = db.get(TournamentGame, game_id)
    if g is None or g.tournament_id != t.id:
        raise HTTPException(status_code=404, detail="Game not found.")
    if g.result == "bye":
        raise HTTPException(status_code=409, detail="A bye has no result to set.")

    if body.result is not None or "result" in body.model_fields_set:
        if body.result not in (None, "a", "b", "draw"):
            raise HTTPException(status_code=422, detail="Result must be a, b, draw or empty.")
        g.result = body.result
        g.reported_by_user_id = user.id
        g.confirmed = True
    if body.a_score is not None:
        g.a_score = body.a_score
    if body.b_score is not None:
        g.b_score = body.b_score
    for f in ("a_sports", "b_sports"):
        v = getattr(body, f)
        if v is not None:
            cap = scoring.config(t)["sports_scale_max"]
            if not (0 <= v <= cap):
                raise HTTPException(
                    status_code=422,
                    detail=f"Sportsmanship must be between 0 and {cap}.")
            setattr(g, f, v)
    if body.table_label is not None:
        g.table_label = body.table_label.strip() or None

    g.updated_at = datetime.utcnow()
    db.add(g)
    db.commit()

    names = {e.id: e.display_name for e in _entries(db, t.id)}
    return {"ok": True, "game": _game_dict(db, g, names), "standings": _standings(db, t)}


# ---------------------------------------------------------------------------
# Player self-service
# ---------------------------------------------------------------------------

def _my_entry(db: Session, t: Tournament, user: User, club_id: int) -> TournamentEntry:
    pid = active_player_id_for(db, user, club_id)
    e = next((e for e in _entries(db, t.id)
              if e.user_id == user.id or (pid and e.player_id == pid)), None)
    if e is None:
        raise HTTPException(status_code=403, detail="You're not entered in this event.")
    return e


class ReportBody(BaseModel):
    my_score: Optional[int] = None
    their_score: Optional[int] = None
    result: Optional[str] = None          # "win" | "loss" | "draw"
    sportsmanship: Optional[int] = None   # what I'm giving my opponent


@router.post("/{tournament_id}/games/{game_id}/report")
def report_result(
    tournament_id: int,
    game_id: int,
    body: ReportBody,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """A player reporting their OWN game.

    Everything is expressed from the reporter's side — "my score", "I won" —
    and mapped onto the game's a/b slots here, so a player never has to work out
    whether they are player A. Refuses any game they aren't in, which is the
    whole point of this existing separately from the TO's endpoint.

    A player report is not `confirmed`. The TO's console shows it as reported
    and can override it, so a disputed score never stalls a round.
    """
    _gate(user)
    t = _get(db, club_id, tournament_id)
    mine = _my_entry(db, t, user, club_id)

    g = db.get(TournamentGame, game_id)
    if g is None or g.tournament_id != t.id:
        raise HTTPException(status_code=404, detail="Game not found.")
    if mine.id not in (g.a_entry_id, g.b_entry_id):
        raise HTTPException(status_code=403, detail="That isn't your game.")
    if g.result == "bye":
        raise HTTPException(status_code=409, detail="A bye has no result to report.")
    if g.confirmed and g.result:
        raise HTTPException(
            status_code=409,
            detail="The organiser has already confirmed this result. Speak to them to change it.",
        )

    i_am_a = g.a_entry_id == mine.id
    if body.result is not None:
        if body.result not in ("win", "loss", "draw"):
            raise HTTPException(status_code=422, detail="Result must be win, loss or draw.")
        g.result = ("draw" if body.result == "draw"
                    else ("a" if (body.result == "win") == i_am_a else "b"))
    if body.my_score is not None:
        setattr(g, "a_score" if i_am_a else "b_score", body.my_score)
    if body.their_score is not None:
        setattr(g, "b_score" if i_am_a else "a_score", body.their_score)
    if body.sportsmanship is not None:
        cap = scoring.config(t)["sports_scale_max"]
        if not (0 <= body.sportsmanship <= cap):
            raise HTTPException(status_code=422, detail=f"Sportsmanship is 0 to {cap}.")
        # I rate my OPPONENT, so it lands on their side of the row.
        setattr(g, "b_sports" if i_am_a else "a_sports", body.sportsmanship)

    g.reported_by_user_id = user.id
    g.confirmed = False
    g.updated_at = datetime.utcnow()
    db.add(g)
    db.commit()
    return {"ok": True}


class MyListBody(BaseModel):
    army_list: Optional[str] = None
    faction: Optional[str] = None


@router.post("/{tournament_id}/my-list")
def submit_my_list(
    tournament_id: int,
    body: MyListBody,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Submit or update your own army list. Free text on purpose — validating a
    list is system-specific, genuinely large, and every TO already uses a
    dedicated tool for it."""
    _gate(user)
    t = _get(db, club_id, tournament_id)
    e = _my_entry(db, t, user, club_id)
    if body.army_list is not None:
        e.army_list = body.army_list.strip() or None
        e.list_submitted_at = datetime.utcnow() if e.army_list else None
    if body.faction is not None:
        e.faction = body.faction.strip() or None
    e.updated_at = datetime.utcnow()
    db.add(e)
    db.commit()
    return {"ok": True, "list_submitted": e.list_submitted_at is not None}


@router.get("/{tournament_id}/standings")
def get_standings(
    tournament_id: int,
    user: Optional[User] = Depends(current_user),
    club_id: int = Depends(public_club_id),
    db: Session = Depends(get_session),
):
    _gate(user)
    t = _get(db, club_id, tournament_id)
    return {
        "standings": _standings(db, t),
        # Shipped alongside the numbers on purpose: the sort key belongs next to
        # the table it sorted.
        "scoring_explained": scoring.describe(t),
    }
