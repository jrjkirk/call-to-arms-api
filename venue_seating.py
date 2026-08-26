"""Where the pairings meet the room.

Call to Arms knows who is playing whom. The venue side knows what furniture
exists and who has booked it. Until now those two facts never met, so a club
night held ten tables every week whether six people turned up or twenty, and
the tables it didn't need sat empty while the venue turned bookings away.

This module is the join:

    what tonight NEEDS   pairings with an opponent, one game to a table
    where it GOES        each game assigned to a real, named table
    what is now SPARE    held tables the night turns out not to need

None of it reaches the players. The pairings post says who you're playing; it
does not say where to sit, because a table number in a Discord message is one
more thing to go stale the moment staff move a game. This is the venue's own
view of its own floor — see VenueSeating's docstring.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlmodel import Session, select

import venue as V
from models import (
    ClubSystem,
    Pairing,
    PublishState,
    Signup,
    SystemConfig,
    VenueClubNight,
    VenueSeat,
    VenueSeating,
    VenueTable,
)
from week_logic import _fmt


# A club night occupies its tables for the evening, not for a slot. Where the
# venue's closing time is known we run to it; otherwise this is the fallback
# length, matching the four hours the booking form already allows as a max.
DEFAULT_NIGHT_MINUTES = 240


def night_window(db: Session, club_id: int, night: VenueClubNight,
                 day: date, start_time: Optional[str] = None) -> tuple[int, int]:
    """[start, end) in minutes for a club night, for checking table clashes."""
    raw = start_time or night.start_time
    if raw is None and night.system_id is not None:
        cs = db.exec(
            select(ClubSystem)
            .where(ClubSystem.club_id == club_id)
            .where(ClubSystem.system_id == night.system_id)
        ).first()
        raw = cs.session_start_time if cs else None
    try:
        start = V.to_minutes(raw) if raw else 18 * 60
    except ValueError:
        start = 18 * 60

    hours = V.hours_for(db, club_id, day)
    end = hours[1] if hours and hours[1] > start else start + DEFAULT_NIGHT_MINUTES
    return start, min(end, 24 * 60)


def _system_for(db: Session, club_id: int, night: VenueClubNight):
    if night.system_id is None:
        return None
    return db.get(SystemConfig, night.system_id)


def pairing_context(db: Session, club_id: int, night: VenueClubNight, day: date) -> dict:
    """What this night's pairings say about the tables it needs on `day`.

    Games rather than signups: a signup is an intention, a pairing is two
    people who will be sitting opposite each other. A BYE takes no table and is
    counted separately, so the venue isn't asked to lay out a board for someone
    who has nobody to play.
    """
    sc = _system_for(db, club_id, night)
    if sc is None:
        # Magic, Bolt Action — this app doesn't run them and never sees who
        # played whom, so there is nothing to lay out from.
        return {"measurable": False, "week": _fmt(day), "system": None,
                "published": False, "games": [], "byes": [], "tables_needed": 0}

    week = _fmt(day)
    pairings = db.exec(
        select(Pairing)
        .where(Pairing.club_id == club_id)
        .where(Pairing.system == sc.legacy_system_name)
        .where(Pairing.week == week)
        .order_by(Pairing.id)
    ).all()

    gate = db.exec(
        select(PublishState)
        .where(PublishState.club_id == club_id)
        .where(PublishState.system == sc.legacy_system_name)
        .where(PublishState.week == week)
    ).first()

    ids = {p.a_signup_id for p in pairings} | {p.b_signup_id for p in pairings if p.b_signup_id}
    names: dict[int, str] = {}
    factions: dict[int, Optional[str]] = {}
    etas: dict[int, Optional[str]] = {}
    if ids:
        for s in db.exec(select(Signup).where(Signup.id.in_(ids))).all():
            names[s.id] = s.player_name
            factions[s.id] = s.faction
            etas[s.id] = s.eta

    games, byes = [], []
    for p in pairings:
        entry = {
            "pairing_id": p.id,
            "a": names.get(p.a_signup_id, "?"),
            "b": names.get(p.b_signup_id) if p.b_signup_id else None,
            "a_faction": p.a_faction or factions.get(p.a_signup_id),
            "b_faction": p.b_faction or (factions.get(p.b_signup_id) if p.b_signup_id else None),
            # When each of them said they'd arrive. Staff standing at a table at
            # ten past seven want to know whether to give it away or wait.
            "a_eta": etas.get(p.a_signup_id),
            "b_eta": etas.get(p.b_signup_id) if p.b_signup_id else None,
            "prearranged": bool(p.prearranged),
        }
        (games if p.b_signup_id is not None else byes).append(entry)

    return {
        "measurable": True,
        "week": week,
        "system": sc.legacy_system_name,
        "system_name": sc.name,
        # Pairings that exist but aren't published yet still tell the venue what
        # to lay out — staff set the room up before the post goes out, not after.
        "published": bool(gate and gate.published),
        "has_pairings": bool(pairings),
        "games": games,
        "byes": byes,
        "tables_needed": len(games),
    }


def _reading_order(t: VenueTable) -> tuple:
    """Sort a table by where it IS, so seats 1..n run across the room the way
    someone walking it would find them.

    Banded on y rather than sorted on it: two tables in the same row are never
    at exactly the same y, and raw sorting would zig-zag down the room. A venue
    that never drew a plan has every position at 0, and this collapses to the
    manual sort order it already set.
    """
    band = round((t.pos_y or 0) / 6.0)
    return (t.room_id or 0, band, t.pos_x or 0, t.sort_order, t.id)


def candidate_tables(db: Session, club_id: int, night: VenueClubNight, day: date,
                     window: tuple[int, int]) -> list[VenueTable]:
    """Tables this night may seat games on, best first.

    Three tiers, and the order is the whole point of the feature:

      1. HELD for this night. The venue already decided these are its tables.
      2. Preferred for it — right size, right part of the room.
      3. Anything else that's free, because a night that outgrows its plan
         should spill onto real tables rather than report a shortfall while
         eight boards sit empty.

    Tables another night has held on the same day never appear: two club nights
    on one evening must not be handed the same table.
    """
    tables = V.active_tables(db, club_id)
    by_night = V.night_tables(db, club_id)
    mine = by_night.get(night.id, {"preferred": [], "reserved": []})
    held = set(mine["reserved"])
    preferred = set(mine["preferred"])

    # Held by SOMEONE ELSE tonight.
    others: set[int] = set()
    for n in V.club_nights_on(db, club_id, day):
        nid = n["night_id"]
        if nid and nid != night.id:
            others.update(by_night.get(nid, {}).get("reserved", []))

    start, end = window
    taken: set[int] = set()
    for b in V.bookings_on(db, club_id, day):
        try:
            if V._overlaps(start, end, V.to_minutes(b.start_time), V.to_minutes(b.end_time)):
                taken.add(b.table_id)
        except ValueError:
            taken.add(b.table_id)

    def tier(t: VenueTable) -> int:
        if t.id in held:
            return 0
        if t.id in preferred:
            return 1
        return 2

    out = [t for t in tables if t.id not in others and t.id not in taken]
    out.sort(key=lambda t: (tier(t),) + _reading_order(t))
    return out


def get_seating(db: Session, club_id: int, night_id: int, day: date) -> Optional[VenueSeating]:
    return db.exec(
        select(VenueSeating)
        .where(VenueSeating.club_id == club_id)
        .where(VenueSeating.club_night_id == night_id)
        .where(VenueSeating.on_date == day)
    ).first()


def seats_for(db: Session, seating_id: int) -> list[VenueSeat]:
    return db.exec(
        select(VenueSeat).where(VenueSeat.seating_id == seating_id).order_by(VenueSeat.id)
    ).all()


def seatings_on(db: Session, club_id: int, day: date) -> dict[int, VenueSeating]:
    """night_id -> that night's plan for one date."""
    return {
        r.club_night_id: r
        for r in db.exec(
            select(VenueSeating)
            .where(VenueSeating.club_id == club_id)
            .where(VenueSeating.on_date == day)
        ).all()
    }


def seatings_between(db: Session, club_id: int, first: date,
                     last: date) -> dict[tuple[int, date], VenueSeating]:
    """Every plan in a date window, for the month view.

    A calendar is thirty-one days and a venue can run four nights a week, so
    asking per day is a hundred round trips for one screen. venue.range_overview
    exists for exactly this reason; this is its half of the bargain.
    """
    return {
        (r.club_night_id, r.on_date): r
        for r in db.exec(
            select(VenueSeating)
            .where(VenueSeating.club_id == club_id)
            .where(VenueSeating.on_date >= first)
            .where(VenueSeating.on_date <= last)
        ).all()
    }


def seat_tables(db: Session, seating_ids: list[int]) -> dict[int, set[int]]:
    """seating_id -> the tables its games are on. Batched, same reason."""
    out: dict[int, set[int]] = {i: set() for i in seating_ids}
    if not seating_ids:
        return out
    for s in db.exec(select(VenueSeat).where(VenueSeat.seating_id.in_(seating_ids))).all():
        out.setdefault(s.seating_id, set()).add(s.table_id)
    return out


def generate(db: Session, club_id: int, night: VenueClubNight, day: date) -> dict:
    """Lay tonight's games out on tonight's tables.

    Stability is the requirement that shapes this. Staff will hit generate
    again after a late signup, and if the room rearranged itself every time,
    the printed sheet on the door would be a lie and nobody would use it. So:

      * a LOCKED seat never moves — a human put it there;
      * an existing seat keeps its table if that table is still usable;
      * only genuinely new games get handed a table, and they take the best
        one still going.

    Which means the common case — one extra game — adds one table and touches
    nothing else.
    """
    ctx = pairing_context(db, club_id, night, day)
    if not ctx["measurable"]:
        return {"ok": False, "reason": "no_system", **ctx}

    window = night_window(db, club_id, night, day)
    candidates = candidate_tables(db, club_id, night, day, window)
    by_id = {t.id: t for t in candidates}

    seating = get_seating(db, club_id, night.id, day)
    if seating is None:
        seating = VenueSeating(club_id=club_id, club_night_id=night.id, on_date=day,
                               week=ctx["week"], system=ctx["system"])
        db.add(seating)
        db.commit()
        db.refresh(seating)

    live = {g["pairing_id"] for g in ctx["games"]}
    existing = seats_for(db, seating.id)

    # A seat whose game no longer exists — someone dropped out, or pairings were
    # regenerated — is dead weight. Drop it and free its table.
    for s in existing:
        if s.pairing_id not in live:
            db.delete(s)
    existing = [s for s in existing if s.pairing_id in live]

    kept: dict[int, VenueSeat] = {}
    used: set[int] = set()
    for s in existing:
        # A locked seat holds its table even if that table has since been
        # booked or unheld: staff said so, and staff can see the room.
        if s.locked or (s.table_id in by_id and s.table_id not in used):
            kept[s.pairing_id] = s
            used.add(s.table_id)
        else:
            db.delete(s)

    free = [t for t in candidates if t.id not in used]
    seated, unseated = [], []
    for g in ctx["games"]:
        pid = g["pairing_id"]
        if pid in kept:
            seated.append((pid, kept[pid].table_id))
            continue
        if not free:
            unseated.append(pid)
            continue
        t = free.pop(0)
        db.add(VenueSeat(club_id=club_id, seating_id=seating.id, pairing_id=pid, table_id=t.id))
        used.add(t.id)
        seated.append((pid, t.id))

    seating.tables_needed = len(ctx["games"])
    seating.week = ctx["week"]
    seating.system = ctx["system"]
    seating.generated_at = datetime.utcnow()
    db.add(seating)
    db.commit()

    return {"ok": True, "seated": len(seated), "unseated": len(unseated), **ctx}


def spare_tables(db: Session, club_id: int, night: VenueClubNight, day: date,
                 seating: Optional[VenueSeating] = None) -> list[int]:
    """Tables held for this night that tonight's games don't need, and that the
    venue could actually sell.

    Held, minus seated, minus two kinds of table that are not the venue's to
    offer even though the arithmetic says otherwise:

      already booked — a held table can still carry a staff booking or an
        event. Counting it as spare would tell staff they have four tables to
        sell when they have three, which is exactly the sort of number that
        ends in two parties at one table.
      held by ANOTHER night meeting the same evening — Magic's tables are not
        The Old World's to hand back just because The Old World doesn't need
        them.

    Only once a seating exists: before that, "spare" would mean "we haven't
    worked it out yet", which is not something to offer a paying customer.
    """
    seating = seating or get_seating(db, club_id, night.id, day)
    if seating is None:
        return []
    by_night = V.night_tables(db, club_id)
    held = set(by_night.get(night.id, {}).get("reserved", []))
    if not held:
        return []

    used = {s.table_id for s in seats_for(db, seating.id)}

    start, end = night_window(db, club_id, night, day)
    booked: set[int] = set()
    for b in V.bookings_on(db, club_id, day):
        try:
            if V._overlaps(start, end, V.to_minutes(b.start_time), V.to_minutes(b.end_time)):
                booked.add(b.table_id)
        except ValueError:
            booked.add(b.table_id)

    others: set[int] = set()
    for n in V.club_nights_on(db, club_id, day):
        nid = n["night_id"]
        if nid and nid != night.id:
            others.update(by_night.get(nid, {}).get("reserved", []))

    return sorted(held - used - booked - others)


def released_table_ids_on(db: Session, club_id: int, day: date) -> set[int]:
    """Tables handed back to the public on one date.

    Read by venue.reserved_table_ids_on, so a released table becomes bookable
    everywhere at once — the public form, the availability grid, the staff
    diary — rather than in whichever screen remembered to ask.
    """
    rows = db.exec(
        select(VenueSeating)
        .where(VenueSeating.club_id == club_id)
        .where(VenueSeating.on_date == day)
        .where(VenueSeating.released == True)
    ).all()
    if not rows:
        return set()

    out: set[int] = set()
    running = {n["night_id"] for n in V.club_nights_on(db, club_id, day) if n["night_id"]}
    for seating in rows:
        if seating.club_night_id not in running:
            continue
        night = db.get(VenueClubNight, seating.club_night_id)
        if night is None:
            continue
        out.update(spare_tables(db, club_id, night, day, seating))
    return out


def seated_table_ids_on(db: Session, club_id: int, day: date) -> dict[int, dict]:
    """table_id -> the game sitting on it, for every club night meeting today.

    Powers the "required tonight" flag on the diary: a seated table is one the
    club night is definitely using, as opposed to one it is merely holding.
    """
    running = {n["night_id"]: n for n in V.club_nights_on(db, club_id, day) if n["night_id"]}
    if not running:
        return {}

    rows = db.exec(
        select(VenueSeating)
        .where(VenueSeating.club_id == club_id)
        .where(VenueSeating.on_date == day)
    ).all()

    out: dict[int, dict] = {}
    for seating in rows:
        if seating.club_night_id not in running:
            continue
        night = running[seating.club_night_id]
        n = db.get(VenueClubNight, seating.club_night_id)
        ctx = pairing_context(db, club_id, n, day) if n else {"games": []}
        games = {g["pairing_id"]: g for g in ctx["games"]}
        for s in seats_for(db, seating.id):
            g = games.get(s.pairing_id)
            if g is None:
                continue
            out[s.table_id] = {
                "night_id": seating.club_night_id,
                "night": night["system"],
                "system": night["system"],
                "color": night["color"],
                "start_time": night["start_time"],
                "a": g["a"],
                "b": g["b"],
                "a_faction": g["a_faction"],
                "b_faction": g["b_faction"],
                "a_eta": g["a_eta"],
                "b_eta": g["b_eta"],
                "pairing_id": g["pairing_id"],
                "locked": bool(s.locked),
            }
    return out


def view(db: Session, club_id: int, night: VenueClubNight, day: date) -> dict:
    """Everything one night's seating screen needs, in one call."""
    ctx = pairing_context(db, club_id, night, day)
    seating = get_seating(db, club_id, night.id, day)
    tables = {t.id: t for t in V.active_tables(db, club_id)}
    held = sorted(V.night_tables(db, club_id).get(night.id, {}).get("reserved", []))

    seats = {s.pairing_id: s for s in seats_for(db, seating.id)} if seating else {}
    rows = []
    for g in ctx["games"]:
        s = seats.get(g["pairing_id"])
        t = tables.get(s.table_id) if s else None
        rows.append({
            **g,
            "table_id": s.table_id if s else None,
            "table": t.name if t else None,
            "table_size": t.size_label if t else None,
            "locked": bool(s.locked) if s else False,
        })

    spare = spare_tables(db, club_id, night, day, seating) if seating else []
    window = night_window(db, club_id, night, day)

    return {
        "night_id": night.id,
        "date": day.isoformat(),
        "window": {"start": V.to_hhmm(window[0]), "end": V.to_hhmm(window[1])},
        "measurable": ctx["measurable"],
        "published": ctx.get("published", False),
        "has_pairings": ctx.get("has_pairings", False),
        "week": ctx["week"],
        "system_name": ctx.get("system_name"),
        "tables_needed": ctx["tables_needed"],
        "tables_held": len(held),
        "byes": ctx["byes"],
        "games": rows,
        "unseated": [r["pairing_id"] for r in rows if r["table_id"] is None],
        "spare_table_ids": spare,
        "spare_tables": [tables[i].name for i in spare if i in tables],
        "released": bool(seating and seating.released),
        "generated_at": seating.generated_at.isoformat() if seating else None,
        # Every table staff can move a game onto, so the picker offers real
        # choices rather than a free-text box.
        "table_options": [
            {"id": t.id, "name": t.name, "size": t.size_label}
            for t in candidate_tables(db, club_id, night, day, window)
        ],
    }


def lay_out_on_publish(db: Session, club_id: int, system: str, week: str) -> Optional[dict]:
    """Lay the room out the moment a week's pairings are published.

    Venue staff shouldn't have to know that pairings happened, let alone press
    a button about it. The pairings ARE the answer to "how many tables does
    tonight need", so publishing them is exactly when the room can be laid out,
    and the diary should simply be right when someone opens it.

    Idempotent by construction: generate() keeps every seat that's still valid
    and moves nobody, so publishing twice — or publishing, editing, publishing
    again — adds the new games and leaves the rest alone.

    `week` is the pairing key ("02/09/2026") and also the date the night runs,
    which is what makes this cheap: no calendar arithmetic, just the night whose
    system matches.

    Never raises. A venue-side convenience must not be able to fail a publish —
    that would be an admin unable to release pairings because a floor plan is
    misconfigured.
    """
    try:
        sc = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == system)).first()
        if sc is None:
            return None
        night = V.night_for_system(db, club_id, sc.id)
        if night is None:
            return None

        day = datetime.strptime(week, "%d/%m/%Y").date()
        # Only if the night actually meets that day. A pairing week is normally
        # the session date, but nothing enforces it, and laying out a Wednesday
        # club night on a Tuesday would put phantom games on the diary.
        if not any(n["night_id"] == night.id for n in V.club_nights_on(db, club_id, day)):
            return None

        result = generate(db, club_id, night, day)
        return result if result.get("ok") else None
    except Exception:
        return None
