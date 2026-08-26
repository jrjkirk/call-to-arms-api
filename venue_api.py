"""Venue booking endpoints: the public booking flow and the staff console.

Split from admin.py rather than added to it. Venue Admin is its own surface
with its own access rule (VenueStaff, not a system scope) — the person running
the bar holds no game-system rights at all — and admin.py is already 3,800
lines of club/system administration.

Two audiences, one router:
  /venue/*        the player booking a table (login required, no admin rights)
  /venue/admin/*  staff (can_admin_venue)
"""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

import venue as V
from auth import active_club_id, current_user, require_user
from database import get_session
from models import (
    Club, SystemConfig, ClubSystem, User, VenueBooking, VenueClubNight,
    VenueConfig, VenueNightTable, VenueSeat, VenueStaff, VenueTable, Player,
)
from database import active_player_id_for

router = APIRouter(prefix="/venue", tags=["venue"])


def _require_venue_admin(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
) -> tuple[User, int]:
    if not V.can_admin_venue(db, user, club_id):
        raise HTTPException(status_code=403, detail="Venue admin access required.")
    return user, club_id


def _is_venue_owner(user: User, club_id: int) -> bool:
    """Whether this user owns the venue, as opposed to working in it.

    Super-admin is a home-club power (see auth.admin_scopes): a super-admin of
    one club is a plain punter at every other, and must not get to approve
    another venue's events.
    """
    return bool(
        user.is_platform_admin or (user.is_super_admin and user.club_id == club_id)
    )


def _require_venue_owner(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
) -> tuple[User, int]:
    """Stricter than _require_venue_admin: the club's own super-admin, or a
    platform admin. Granting venue access is an ownership decision, not a
    day-to-day one — the bar manager runs the diary but doesn't get to hand out
    keys, and shouldn't be able to promote themselves' colleagues either."""
    if _is_venue_owner(user, club_id):
        return user, club_id
    raise HTTPException(
        status_code=403,
        detail="Only a club super-admin can do that.",
    )


def _require_enabled(db: Session, club_id: int) -> VenueConfig:
    """Public endpoints 404 rather than 403 when a club doesn't sell table
    space. There is nothing to authorize — the feature simply isn't there for
    that club, and saying "forbidden" would imply it exists and they can't
    have it."""
    cfg = V.get_config(db, club_id)
    if cfg is None or not cfg.enabled:
        raise HTTPException(status_code=404, detail="This club doesn't take table bookings.")
    return cfg


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

@router.get("/info")
def venue_info(
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """What the booking page needs to render itself: policy, the systems this
    venue runs, and how far ahead it takes bookings."""
    cfg = V.get_config(db, club_id)
    if cfg is None or not cfg.enabled:
        return {"enabled": False}

    club = db.get(Club, club_id)
    systems = db.exec(
        select(SystemConfig)
        .join(ClubSystem, ClubSystem.system_id == SystemConfig.id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
        .order_by(SystemConfig.name)
    ).all()
    today = V.club_now(db, club_id).date()

    return {
        "enabled": True,
        "club_name": club.name if club else None,
        "address": club.address if club else None,
        "booking_blurb": cfg.booking_blurb,
        "confirm_mode": cfg.confirm_mode,
        "slot_minutes": cfg.slot_minutes,
        "min_duration_minutes": cfg.min_duration_minutes,
        "max_duration_minutes": cfg.max_duration_minutes,
        "max_party_size": cfg.max_party_size,
        "first_date": today.isoformat(),
        "last_date": (today + timedelta(days=cfg.max_advance_days)).isoformat(),
        "opening_hours": V.opening_hours(db, club_id),
        "tables": len(V.active_tables(db, club_id)),
        "systems": [{"id": s.id, "name": s.name, "slug": s.slug} for s in systems],
    }


@router.get("/availability")
def get_availability(
    date_str: str = Query(..., alias="date"),
    duration: Optional[int] = None,
    party_size: Optional[int] = None,
    system_id: Optional[int] = None,
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    _require_enabled(db, club_id)
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.")
    return V.availability(db, club_id, day, duration, party_size, system_id)


@router.get("/tables-for-slot")
def tables_for_slot(
    date_str: str = Query(..., alias="date"),
    start_time: str = Query(...),
    duration: int = Query(...),
    party_size: int = Query(2),
    system_id: Optional[int] = None,
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """The actual tables free for one slot, best first, flagged for whether they
    suit the chosen game.

    Lets the booking form offer a real choice — "Table 3, 6x4, recommended for
    The Old World" — instead of silently assigning something and hoping. The
    first entry is what the venue would pick, so a booker who doesn't care can
    ignore the list entirely.
    """
    _require_enabled(db, club_id)
    try:
        day = date.fromisoformat(date_str)
        start = V.to_minutes(start_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Bad date or time.")

    preferred = set(V.tables_for_system(db, club_id, system_id))
    free = V.free_tables_for(
        db, club_id, day, start, start + duration,
        party_size=party_size, system_id=system_id,
    )
    return {
        "tables": [
            {"id": t.id, "name": t.name, "size_label": t.size_label, "seats": t.seats,
             "recommended": t.id in preferred}
            for t in free
        ],
        "held_for_club_night": sorted(V.reserved_table_ids_on(db, club_id, day)),
    }


@router.get("/busy")
def get_busy(
    start: str = Query(...),
    days: int = Query(14, ge=1, le=62),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """How busy each of the next N days is — the "see how busy a night is"
    view. Public, because a booker choosing a date deserves the same picture
    staff have: a Wednesday already carrying an eighteen-player club night is
    not the quiet midweek slot it looks like on an empty grid."""
    _require_enabled(db, club_id)
    try:
        first = date.fromisoformat(start)
    except ValueError:
        raise HTTPException(status_code=422, detail="start must be YYYY-MM-DD.")
    return {"days": [V.day_overview(db, club_id, first + timedelta(days=i))
                     for i in range(days)]}


class CreateBookingBody(BaseModel):
    date: str
    start_time: str
    duration_minutes: int
    party_size: int = 2
    system_id: Optional[int] = None
    game_note: Optional[str] = None
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None
    table_id: Optional[int] = None


@router.post("/bookings")
def create_booking(
    body: CreateBookingBody,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Book a table. Login required, so the account is the abuse control: no
    rate limiting by IP, no email-confirmation round trip, and a real person to
    talk to if they no-show.

    Every constraint is re-checked here rather than trusted from the grid the
    browser was shown — that grid may be minutes old, and two people can want
    the same table at once.
    """
    cfg = _require_enabled(db, club_id)

    try:
        day = date.fromisoformat(body.date)
        start = V.to_minutes(body.start_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Bad date or time.")

    duration = int(body.duration_minutes)
    if duration < cfg.min_duration_minutes or duration > cfg.max_duration_minutes:
        raise HTTPException(
            status_code=422,
            detail=f"Bookings run from {cfg.min_duration_minutes} to "
                   f"{cfg.max_duration_minutes} minutes.",
        )
    if duration % cfg.slot_minutes:
        raise HTTPException(
            status_code=422,
            detail=f"Length must be a multiple of {cfg.slot_minutes} minutes.",
        )
    if start % cfg.slot_minutes:
        raise HTTPException(
            status_code=422,
            detail=f"Start time must be on a {cfg.slot_minutes}-minute boundary.",
        )
    if not (1 <= body.party_size <= cfg.max_party_size):
        raise HTTPException(
            status_code=422, detail=f"Party size must be 1 to {cfg.max_party_size}."
        )

    end = start + duration
    window = V.hours_for(db, club_id, day)
    if window is None:
        raise HTTPException(status_code=409, detail="The venue isn't open for bookings that day.")
    if start < window[0] or end > window[1]:
        raise HTTPException(
            status_code=409,
            detail=f"Bookings that day run {V.to_hhmm(window[0])}–{V.to_hhmm(window[1])}.",
        )

    now = V.club_now(db, club_id)
    if day < now.date() or (
        day == now.date() and start < now.hour * 60 + now.minute + cfg.lead_time_minutes
    ):
        raise HTTPException(status_code=409, detail="That slot is too close to now.")
    if day > now.date() + timedelta(days=cfg.max_advance_days):
        raise HTTPException(
            status_code=409,
            detail=f"Bookings open {cfg.max_advance_days} days ahead.",
        )

    held = db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.user_id == user.id)
        .where(VenueBooking.booking_date >= now.date())
        .where(VenueBooking.status.in_(V.BLOCKING_STATUSES))
    ).all()
    if len(held) >= cfg.max_active_bookings_per_user:
        raise HTTPException(
            status_code=409,
            detail=f"You already have {len(held)} upcoming bookings here "
                   f"({cfg.max_active_bookings_per_user} is the limit). "
                   f"Cancel one to book another.",
        )

    free = V.free_tables_for(
        db, club_id, day, start, end,
        party_size=body.party_size, system_id=body.system_id,
    )
    if not free:
        raise HTTPException(status_code=409, detail="No table free for that slot.")
    if body.table_id is not None:
        chosen = next((t for t in free if t.id == body.table_id), None)
        if chosen is None:
            raise HTTPException(status_code=409, detail="That table isn't free for that slot.")
    else:
        # free_tables_for already returns best-first: tables that suit the game,
        # then the smallest that still fits, so a pair doesn't eat the only 6x4.
        chosen = free[0]

    player_id = active_player_id_for(db, user, club_id)
    player = db.get(Player, player_id) if player_id else None
    name = (body.contact_name or "").strip() or (player.name if player else None) \
        or user.discord_name or "Guest"

    booking = VenueBooking(
        club_id=club_id,
        table_id=chosen.id,
        booking_date=day,
        start_time=V.to_hhmm(start),
        end_time=V.to_hhmm(end),
        party_size=body.party_size,
        system_id=body.system_id,
        game_note=(body.game_note or "").strip() or None,
        user_id=user.id,
        player_id=player_id,
        contact_name=name,
        contact_email=(body.contact_email or "").strip() or None,
        contact_phone=(body.contact_phone or "").strip() or None,
        notes=(body.notes or "").strip() or None,
        status="requested" if cfg.confirm_mode == "request" else "confirmed",
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)

    # After the commit on purpose: the booking is the record and must survive a
    # slow Resend call or a revoked webhook. notify_staff never raises.
    delivery = V.notify_staff(db, club_id, booking)

    return {
        "ok": True,
        "booking": V.describe_booking(db, booking),
        "notified": delivery,
        "club_night": V.club_night_pitch(db, club_id, booking),
    }


@router.get("/bookings/mine")
def my_bookings(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    today = V.club_now(db, club_id).date()
    rows = db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.user_id == user.id)
        .where(VenueBooking.booking_date >= today)
        .order_by(VenueBooking.booking_date, VenueBooking.start_time)
    ).all()
    return [V.describe_booking(db, b) for b in rows]


@router.delete("/bookings/{booking_id}")
def cancel_booking(
    booking_id: int,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Cancel your own booking. Staff cancel through the console instead, so
    this checks ownership and nothing else — a venue admin cancelling someone's
    table should leave a staff-attributed record, not look like the booker
    changed their mind."""
    booking = db.get(VenueBooking, booking_id)
    if booking is None or booking.club_id != club_id:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if booking.user_id != user.id:
        raise HTTPException(status_code=403, detail="That isn't your booking.")
    if booking.status == "cancelled":
        return {"ok": True, "already": True}

    booking.status = "cancelled"
    booking.cancelled_at = datetime.utcnow()
    booking.cancelled_by_user_id = user.id
    booking.updated_at = datetime.utcnow()
    db.add(booking)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Staff console
# ---------------------------------------------------------------------------

@router.get("/admin/me")
def venue_admin_me(
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Always 200 — the frontend asks this on every page load to decide whether
    to show the Venue Admin tab, and a 403 there would be a console error on
    every ordinary player's screen."""
    from auth import resolve_active_club_id
    if user is None:
        return {"can_admin_venue": False, "enabled": False}
    club_id = resolve_active_club_id(db, user, None)
    cfg = V.get_config(db, club_id)
    return {
        "can_admin_venue": V.can_admin_venue(db, user, club_id),
        # Separate from the above so the page can hide the Staff tab from a bar
        # manager rather than showing them a tab that 403s.
        "can_manage_staff": bool(
            user.is_platform_admin or (user.is_super_admin and user.club_id == club_id)
        ),
        "enabled": bool(cfg and cfg.enabled),
    }


@router.get("/admin/config")
def get_venue_config(ctx=Depends(_require_venue_admin), db: Session = Depends(get_session)):
    _, club_id = ctx
    cfg = V.get_or_create_config(db, club_id)
    club = db.get(Club, club_id)
    return {
        "enabled": cfg.enabled,
        "confirm_mode": cfg.confirm_mode,
        "slot_minutes": cfg.slot_minutes,
        "min_duration_minutes": cfg.min_duration_minutes,
        "max_duration_minutes": cfg.max_duration_minutes,
        "max_advance_days": cfg.max_advance_days,
        "lead_time_minutes": cfg.lead_time_minutes,
        "max_party_size": cfg.max_party_size,
        "max_active_bookings_per_user": cfg.max_active_bookings_per_user,
        "notify_email": cfg.notify_email,
        "notify_emails": cfg.notify_emails or [],
        "notify_discord": cfg.notify_discord,
        "booking_blurb": cfg.booking_blurb,
        "confirmation_note": cfg.confirmation_note,
        "promote_club_nights": cfg.promote_club_nights,
        # So the settings screen can warn that email is on but points nowhere,
        # rather than letting the venue find out when a booking goes unseen.
        "effective_emails": V.staff_emails(db, club_id, cfg),
        "discord_webhook_configured": _venue_webhook_set(db, club_id),
    }


def _venue_webhook_set(db: Session, club_id: int) -> bool:
    from database import resolve_webhook_url
    return bool(resolve_webhook_url(db, club_id, V.WEBHOOK_TYPE_VENUE, None))


class VenueConfigBody(BaseModel):
    enabled: Optional[bool] = None
    confirm_mode: Optional[str] = None
    slot_minutes: Optional[int] = None
    min_duration_minutes: Optional[int] = None
    max_duration_minutes: Optional[int] = None
    max_advance_days: Optional[int] = None
    lead_time_minutes: Optional[int] = None
    max_party_size: Optional[int] = None
    max_active_bookings_per_user: Optional[int] = None
    notify_email: Optional[bool] = None
    notify_emails: Optional[list[str]] = None
    notify_discord: Optional[bool] = None
    booking_blurb: Optional[str] = None
    confirmation_note: Optional[str] = None
    promote_club_nights: Optional[bool] = None


@router.post("/admin/config")
def save_venue_config(
    body: VenueConfigBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    _, club_id = ctx
    cfg = V.get_or_create_config(db, club_id)

    if body.confirm_mode is not None:
        if body.confirm_mode not in ("instant", "request"):
            raise HTTPException(status_code=422, detail="confirm_mode must be instant or request.")
        cfg.confirm_mode = body.confirm_mode

    if body.slot_minutes is not None:
        if body.slot_minutes not in (15, 30, 60):
            raise HTTPException(status_code=422, detail="slot_minutes must be 15, 30 or 60.")
        cfg.slot_minutes = body.slot_minutes

    for field, lo, hi in (
        ("min_duration_minutes", 15, 720),
        ("max_duration_minutes", 15, 720),
        ("max_advance_days", 1, 365),
        ("lead_time_minutes", 0, 10080),
        ("max_party_size", 1, 40),
        ("max_active_bookings_per_user", 1, 50),
    ):
        val = getattr(body, field)
        if val is not None:
            if not (lo <= val <= hi):
                raise HTTPException(status_code=422, detail=f"{field} must be {lo}–{hi}.")
            setattr(cfg, field, val)

    if cfg.min_duration_minutes > cfg.max_duration_minutes:
        raise HTTPException(
            status_code=422,
            detail="Shortest booking can't be longer than the longest.",
        )

    for field in ("notify_email", "notify_discord", "promote_club_nights", "enabled"):
        val = getattr(body, field)
        if val is not None:
            setattr(cfg, field, bool(val))

    if body.notify_emails is not None:
        cfg.notify_emails = [e.strip() for e in body.notify_emails if (e or "").strip()] or None
    for field in ("booking_blurb", "confirmation_note"):
        val = getattr(body, field)
        if val is not None:
            setattr(cfg, field, val.strip() or None)

    # Turning the venue on with nothing to sell would publish a booking page
    # that can never take a booking.
    if cfg.enabled and all(r["closed"] for r in V.opening_hours(db, club_id)):
        raise HTTPException(
            status_code=409,
            detail="Set your open hours before opening bookings — every day is "
                   "currently marked closed, so no slot could ever be offered.",
        )
    if cfg.enabled and not V.active_tables(db, club_id):
        raise HTTPException(
            status_code=409,
            detail="Add at least one table before opening bookings.",
        )

    cfg.updated_at = datetime.utcnow()
    db.add(cfg)
    db.commit()
    return get_venue_config(ctx=ctx, db=db)


def _clean_hours(rows: list) -> list:
    """Normalise the weekday grid: one row per day, in week order, with times
    that parse. A malformed row is stored closed rather than dropped — a day
    missing from the list would silently fall through to hours_for()'s None and
    read as closed anyway, so making it explicit keeps the admin form honest
    about what it saved."""
    by_day = {}
    for row in rows or []:
        day = (row.get("day") or "").strip().title()
        if day not in V.WEEKDAYS:
            continue
        note = (row.get("note") or "").strip() or None
        if row.get("closed"):
            by_day[day] = {"day": day, "open": None, "close": None, "closed": True, "note": note}
            continue
        try:
            o, c = V.to_minutes(row.get("open")), V.to_minutes(row.get("close"))
        except (ValueError, TypeError):
            by_day[day] = {"day": day, "open": None, "close": None, "closed": True, "note": note}
            continue
        if c <= o:
            raise HTTPException(
                status_code=422,
                detail=f"{day}: closing time must be after opening time.",
            )
        by_day[day] = {"day": day, "open": V.to_hhmm(o), "close": V.to_hhmm(c),
                       "closed": False, "note": note}
    return [by_day.get(d, {"day": d, "open": None, "close": None, "closed": True, "note": None})
            for d in V.WEEKDAYS]


# ---- tables ----

class TableBody(BaseModel):
    name: str
    size_label: Optional[str] = None
    seats: int = 2
    active: bool = True
    sort_order: int = 0
    notes: Optional[str] = None


@router.get("/admin/tables")
def list_tables(ctx=Depends(_require_venue_admin), db: Session = Depends(get_session)):
    _, club_id = ctx
    rows = sorted(
        db.exec(select(VenueTable).where(VenueTable.club_id == club_id)).all(),
        key=lambda t: (V.natural_key(t.name), t.sort_order, t.id),
    )
    return [
        {"id": t.id, "name": t.name, "size_label": t.size_label, "seats": t.seats,
         "active": t.active, "sort_order": t.sort_order, "notes": t.notes}
        for t in rows
    ]


@router.post("/admin/tables")
def create_table(
    body: TableBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    _, club_id = ctx
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Table needs a name.")
    t = VenueTable(
        club_id=club_id, name=name, size_label=(body.size_label or "").strip() or None,
        seats=max(1, body.seats), active=body.active, sort_order=body.sort_order,
        notes=(body.notes or "").strip() or None,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id": t.id}


@router.patch("/admin/tables/{table_id}")
def patch_table(
    table_id: int,
    body: TableBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    _, club_id = ctx
    t = db.get(VenueTable, table_id)
    if t is None or t.club_id != club_id:
        raise HTTPException(status_code=404, detail="Table not found.")
    t.name = body.name.strip() or t.name
    t.size_label = (body.size_label or "").strip() or None
    t.seats = max(1, body.seats)
    t.active = body.active
    t.sort_order = body.sort_order
    t.notes = (body.notes or "").strip() or None
    db.add(t)
    db.commit()
    # Retiring a table can strand a game sitting on it. See resync_night.
    try:
        from venue_seating import resync_all_nights

        resync_all_nights(db, club_id)
    except Exception:
        pass
    return {"ok": True}


@router.delete("/admin/tables/{table_id}")
def delete_table(
    table_id: int,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Refuses a table with bookings against it, for the same reason player
    deletion refuses a player with games: the bookings would survive pointing
    at a table nobody can name. Deactivate instead — it stops new bookings and
    keeps the history readable."""
    _, club_id = ctx
    t = db.get(VenueTable, table_id)
    if t is None or t.club_id != club_id:
        raise HTTPException(status_code=404, detail="Table not found.")
    used = db.exec(
        select(VenueBooking).where(VenueBooking.table_id == table_id)
    ).first()
    if used is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{t.name} has bookings against it. Turn it off instead of deleting it — "
                   f"that stops new bookings and keeps the old ones readable.",
        )
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---- the diary ----

@router.get("/admin/day")
def admin_day(
    date_str: str = Query(..., alias="date"),
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """One day, in full: every booking on every table, plus the club nights
    running that evening and how much of the room they're expected to take.

    This is the answer to "constantly using judgment to navigate the club night
    bookings" — the judgment call becomes a number staff can see before they
    accept anything.
    """
    _, club_id = ctx
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.")

    overview = V.day_overview(db, club_id, day)
    rows = db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.booking_date == day)
        .order_by(VenueBooking.start_time)
    ).all()
    # Includes inactive tables, so the day's history still names them, but in
    # the same name order as everywhere else — see V.natural_key.
    tables = sorted(
        db.exec(select(VenueTable).where(VenueTable.club_id == club_id)).all(),
        key=lambda t: (V.natural_key(t.name), t.sort_order, t.id),
    )

    return {
        **overview,
        "tables": [
            {"id": t.id, "name": t.name, "size_label": t.size_label,
             "seats": t.seats, "active": t.active}
            for t in tables
        ],
        "booking_rows": [
            {**V.describe_booking(db, b),
             "table_id": b.table_id,
             "event_id": b.event_id,
             "start_time": b.start_time,
             "end_time": b.end_time,
             "created_by_staff": b.created_by_staff,
             "staff_note": b.staff_note}
            for b in rows
        ],
    }


@router.get("/admin/calendar")
def admin_calendar(
    month: str = Query(..., description="YYYY-MM"),
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """A whole month of busyness, for the Diary's calendar view.

    Batched through range_overview rather than looped day by day — thirty-one
    day_overview calls would be several hundred queries for one screen.
    """
    _, club_id = ctx
    try:
        year, mon = (int(p) for p in month.split("-"))
        first = date(year, mon, 1)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM.")
    last = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return {
        "month": month,
        "first_weekday": first.weekday(),   # 0 = Monday, to pad the grid
        "days": V.range_overview(db, club_id, first, last),
    }


@router.get("/admin/upcoming")
def admin_upcoming(
    days: int = Query(14, ge=1, le=62),
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """The next N days at a glance, plus anything waiting on staff. The
    `pending` list is deliberately unbounded by the date window: a request
    sitting unanswered is the one thing that must never scroll off."""
    user, club_id = ctx
    first = V.club_now(db, club_id).date()
    pending = db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.status == "requested")
        .where(VenueBooking.booking_date >= first)
        .order_by(VenueBooking.booking_date, VenueBooking.start_time)
    ).all()
    from models import VenueEvent
    pending_events = db.exec(
        select(VenueEvent)
        .where(VenueEvent.club_id == club_id)
        .where(VenueEvent.status == "pending")
        .where(VenueEvent.event_date >= first)
        .order_by(VenueEvent.event_date, VenueEvent.start_time)
    ).all()
    return {
        "days": V.range_overview(db, club_id, first, first + timedelta(days=days - 1)),
        "pending": [V.describe_booking(db, b) for b in pending],
        # Deliberately not bounded by the date window: an event waiting on an
        # answer is the one thing that must never scroll out of sight, and it's
        # holding the room while it waits.
        "pending_events": [_event_payload(db, e) for e in pending_events],
        # Whether the person looking can actually decide, so the page shows
        # Approve/Reject rather than buttons that would 403.
        "can_approve": _is_venue_owner(user, club_id),
    }


class BookingActionBody(BaseModel):
    status: Optional[str] = None
    staff_note: Optional[str] = None
    table_id: Optional[int] = None


@router.patch("/admin/bookings/{booking_id}")
def admin_patch_booking(
    booking_id: int,
    body: BookingActionBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Confirm, cancel, mark a no-show, move a table, or leave a note."""
    user, club_id = ctx
    b = db.get(VenueBooking, booking_id)
    if b is None or b.club_id != club_id:
        raise HTTPException(status_code=404, detail="Booking not found.")

    if body.table_id is not None and body.table_id != b.table_id:
        target = db.get(VenueTable, body.table_id)
        if target is None or target.club_id != club_id:
            raise HTTPException(status_code=404, detail="Table not found.")
        free = V.free_tables_for(
            db, club_id, b.booking_date,
            V.to_minutes(b.start_time), V.to_minutes(b.end_time),
            exclude_booking_id=b.id,
        )
        if target.id not in {t.id for t in free}:
            raise HTTPException(status_code=409, detail=f"{target.name} is taken for that slot.")
        b.table_id = target.id

    if body.status is not None:
        if body.status not in ("requested", "confirmed", "cancelled", "no_show"):
            raise HTTPException(status_code=422, detail="Unknown status.")
        if body.status == "cancelled" and b.status != "cancelled":
            b.cancelled_at = datetime.utcnow()
            b.cancelled_by_user_id = user.id
        b.status = body.status

    if body.staff_note is not None:
        b.staff_note = body.staff_note.strip() or None

    b.updated_at = datetime.utcnow()
    db.add(b)
    db.commit()
    db.refresh(b)
    return {"ok": True, "booking": V.describe_booking(db, b)}


class StaffBookingBody(CreateBookingBody):
    """Staff booking someone in over the phone or at the door.

    Deliberately skips the lead time, the per-user limit and the advance
    window: those exist to protect staff from the public, and staff standing at
    the bar looking at the table do not need protecting from themselves. The
    table clash check stays — that one is physics.
    """
    contact_name: str


@router.post("/admin/bookings")
def admin_create_booking(
    body: StaffBookingBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    user, club_id = ctx
    cfg = V.get_or_create_config(db, club_id)
    try:
        day = date.fromisoformat(body.date)
        start = V.to_minutes(body.start_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Bad date or time.")
    end = start + int(body.duration_minutes)
    if end <= start:
        raise HTTPException(status_code=422, detail="Length must be positive.")

    # for_public=False: staff may seat someone on a table held for a club night.
    free = V.free_tables_for(
        db, club_id, day, start, end, system_id=body.system_id, for_public=False,
    )
    # A table with a club-night game on it is the last one to hand out. Staff
    # may still pick it deliberately; auto-assignment never should.
    seated = _seated_tables_on(db, club_id, day)
    if body.table_id is not None:
        chosen = next((t for t in free if t.id == body.table_id), None)
        if chosen is None:
            raise HTTPException(status_code=409, detail="That table isn't free for that slot.")
    else:
        candidates = [t for t in free if t.seats >= body.party_size] or free
        if not candidates:
            raise HTTPException(status_code=409, detail="No table free for that slot.")
        chosen = min(candidates,
                     key=lambda t: (t.id in seated, t.seats, V.natural_key(t.name), t.id))

    b = VenueBooking(
        club_id=club_id, table_id=chosen.id, booking_date=day,
        start_time=V.to_hhmm(start), end_time=V.to_hhmm(end),
        party_size=body.party_size, system_id=body.system_id,
        game_note=(body.game_note or "").strip() or None,
        user_id=user.id, player_id=None,
        contact_name=body.contact_name.strip(),
        contact_email=(body.contact_email or "").strip() or None,
        contact_phone=(body.contact_phone or "").strip() or None,
        notes=(body.notes or "").strip() or None,
        status="confirmed", created_by_staff=True,
    )
    db.add(b)
    db.commit()
    db.refresh(b)

    # If this landed on a game's table, move the game rather than leaving two
    # parties with a claim on one board — and say whose game moved.
    displaced_game = seated.get(chosen.id)
    if displaced_game:
        try:
            from venue_seating import resync_all_nights

            resync_all_nights(db, club_id)
        except Exception:
            pass
    # No notification: staff entered this themselves and don't need telling.
    return {
        "ok": True,
        "booking": V.describe_booking(db, b),
        "displaced": (f'{displaced_game["a"]} v {displaced_game["b"]}'
                      if displaced_game else None),
    }


# ---- staff access ----

@router.get("/admin/staff")
def list_staff(ctx=Depends(_require_venue_owner), db: Session = Depends(get_session)):
    """Who holds venue access here. Super-admins and platform admins aren't
    listed — they have it implicitly and can't be revoked from this screen, so
    showing them with a Remove button would be a lie."""
    _, club_id = ctx
    rows = db.exec(select(VenueStaff).where(VenueStaff.club_id == club_id)).all()
    out = []
    for r in rows:
        u = db.get(User, r.user_id)
        pid = active_player_id_for(db, u, club_id) if u else None
        p = db.get(Player, pid) if pid else None
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "discord_name": u.discord_name if u else None,
            "player_name": p.name if p else None,
        })
    return sorted(out, key=lambda r: (r["player_name"] or r["discord_name"] or ""))


class StaffBody(BaseModel):
    user_id: int


@router.post("/admin/staff")
def add_staff(
    body: StaffBody,
    ctx=Depends(_require_venue_owner),
    db: Session = Depends(get_session),
):
    _, club_id = ctx
    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    existing = db.exec(
        select(VenueStaff).where(
            VenueStaff.club_id == club_id, VenueStaff.user_id == body.user_id
        )
    ).first()
    if existing is not None:
        return {"ok": True, "already": True}
    db.add(VenueStaff(club_id=club_id, user_id=body.user_id))
    db.commit()
    return {"ok": True}


@router.delete("/admin/staff/{staff_id}")
def remove_staff(
    staff_id: int,
    ctx=Depends(_require_venue_owner),
    db: Session = Depends(get_session),
):
    _, club_id = ctx
    row = db.get(VenueStaff, staff_id)
    if row is None or row.club_id != club_id:
        raise HTTPException(status_code=404, detail="Not found.")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---- club nights: what each one needs, and which tables are its own ----

def _night_payload(db: Session, club_id: int, night, cs=None, sc=None) -> dict:
    assigned = V.night_tables(db, club_id).get(night.id, {"preferred": [], "reserved": []})
    base = {
        "night_id": night.id,
        "system_id": night.system_id,
        "app_managed": night.system_id is not None,
        "expected_tables": night.expected_tables,
        "notes": night.notes,
        # How this night's held tables are drawn on the Diary. On the row for
        # BOTH kinds of night: the schedule may belong to ClubSystem, but the
        # colour is the venue's own call about its own floor.
        "color": night.color,
        "preferred_table_ids": sorted(assigned["preferred"]),
        "reserved_table_ids": sorted(assigned["reserved"]),
        "review": V.table_review(db, club_id, night),
    }
    if night.system_id is not None and cs is not None and sc is not None:
        # Schedule comes from ClubSystem, never from the venue row — one source
        # of truth, so the venue can't set a day that contradicts the game night.
        base.update({
            "system": sc.name,
            "session_day": cs.session_day,
            "session_cadence": cs.session_cadence,
            "start_time": cs.session_start_time,
            "editable_schedule": False,
        })
    else:
        base.update({
            "system": night.name or "Club night",
            "session_day": night.session_day,
            "session_cadence": night.session_cadence or "weekly",
            "cadence_anchor": night.cadence_anchor.isoformat() if night.cadence_anchor else None,
            "start_time": night.start_time,
            "editable_schedule": True,
        })
    return base


@router.get("/admin/club-nights")
def get_club_nights(ctx=Depends(_require_venue_admin), db: Session = Depends(get_session)):
    """Every night this venue hosts — the ones Call to Arms runs and the ones it
    doesn't — with each one's table plan, its tables, and how the plan has held
    up where that can be measured."""
    _, club_id = ctx

    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
        .order_by(SystemConfig.name)
    ).all()

    out = []
    for cs, sc in rows:
        night = V.get_or_create_night_for_system(db, club_id, sc.id)
        out.append(_night_payload(db, club_id, night, cs, sc))

    for night in V.club_nights(db, club_id):
        if night.system_id is None:
            out.append(_night_payload(db, club_id, night))

    # Includes inactive tables, so the day's history still names them, but in
    # the same name order as everywhere else — see V.natural_key.
    tables = sorted(
        db.exec(select(VenueTable).where(VenueTable.club_id == club_id)).all(),
        key=lambda t: (V.natural_key(t.name), t.sort_order, t.id),
    )
    return {
        "club_nights": out,
        "tables": [
            {"id": t.id, "name": t.name, "size_label": t.size_label,
             "seats": t.seats, "active": t.active}
            for t in tables
        ],
    }


class ClubNightBody(BaseModel):
    night_id: Optional[int] = None
    # Venue-only nights: the whole record of the night, since nothing else
    # anywhere knows they exist.
    name: Optional[str] = None
    session_day: Optional[str] = None
    session_cadence: Optional[str] = None
    cadence_anchor: Optional[str] = None
    start_time: Optional[str] = None

    expected_tables: Optional[int] = None
    color: Optional[str] = None
    notes: Optional[str] = None
    # Every table this night may use, and the subset held back from the public
    # when it runs. Sent whole rather than as add/remove operations — the admin
    # screen is a grid of chips, and replacing the set is the only way for
    # clearing the last one to mean anything.
    preferred_table_ids: Optional[list[int]] = None
    reserved_table_ids: Optional[list[int]] = None


@router.post("/admin/club-nights")
def save_club_night(
    body: ClubNightBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Create or update a night. Omitting night_id creates a VENUE-ONLY night —
    Magic, Bolt Action, anything this app doesn't run — which is the only kind
    that can be created here. Call to Arms nights get their row made for them
    when the tab is first opened, because their schedule already exists."""
    _, club_id = ctx

    if body.night_id is None:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Give the night a name.")
        night = VenueClubNight(club_id=club_id, system_id=None, name=name)
    else:
        night = db.get(VenueClubNight, body.night_id)
        if night is None or night.club_id != club_id:
            raise HTTPException(status_code=404, detail="Club night not found.")
        if body.name is not None and night.system_id is None:
            name = body.name.strip()
            if not name:
                raise HTTPException(status_code=422, detail="Give the night a name.")
            night.name = name

    if night.system_id is None:
        # Schedule is only ours to set for a venue-only night; for a Call to
        # Arms one it lives on ClubSystem and the venue must not fork it.
        if body.session_day is not None:
            if body.session_day not in V.WEEKDAYS:
                raise HTTPException(status_code=422, detail="Pick a day of the week.")
            night.session_day = body.session_day
        if body.session_cadence is not None:
            if body.session_cadence not in ("weekly", "fortnightly", "monthly"):
                raise HTTPException(
                    status_code=422,
                    detail="Cadence must be weekly, fortnightly or monthly.")
            night.session_cadence = body.session_cadence
        if body.cadence_anchor is not None:
            try:
                night.cadence_anchor = date.fromisoformat(body.cadence_anchor) \
                    if body.cadence_anchor else None
            except ValueError:
                raise HTTPException(status_code=422, detail="Anchor date must be YYYY-MM-DD.")
        if body.start_time is not None:
            raw = body.start_time.strip()
            if raw:
                try:
                    V.to_minutes(raw)
                except ValueError:
                    raise HTTPException(status_code=422, detail="Start time must be HH:MM.")
            night.start_time = raw or None
        if not night.session_day:
            raise HTTPException(status_code=422, detail="Pick the day this night runs.")
        # A fortnightly night with no anchor can't be placed on a calendar at
        # all, so it would silently never appear. Refuse it at the door.
        # Both need an anchor, for different reasons: a fortnightly night to
        # know which weeks it falls on, a monthly one to know WHICH weekday of
        # the month it is — the ordinal is read off the date it last ran rather
        # than asked for separately.
        if (night.session_cadence or "weekly") in ("fortnightly", "monthly") \
                and night.cadence_anchor is None:
            raise HTTPException(
                status_code=422,
                detail="A fortnightly or monthly night needs a date it last ran, so we know "
                       "which weeks it falls on.",
            )

    if body.expected_tables is not None and not (0 <= body.expected_tables <= 200):
        raise HTTPException(status_code=422, detail="Expected tables must be 0–200.")
    night.expected_tables = body.expected_tables
    # Both kinds of night own their colour — unlike the schedule, it says
    # nothing about the game, only about how the venue reads its own floor.
    if body.color is not None:
        night.color = body.color if body.color in V.TABLE_COLORS else "amber"
    if body.notes is not None:
        night.notes = body.notes.strip() or None
    night.updated_at = datetime.utcnow()
    db.add(night)
    db.commit()
    db.refresh(night)

    if body.preferred_table_ids is not None or body.reserved_table_ids is not None:
        preferred = set(body.preferred_table_ids or [])
        reserved = set(body.reserved_table_ids or [])
        # Holding a table is a stronger form of preferring it, so a held table
        # is always preferred too. Without this a venue could hold a table for a
        # night and simultaneously mark it unsuitable for that night.
        preferred |= reserved

        valid = {
            t.id for t in db.exec(
                select(VenueTable).where(VenueTable.club_id == club_id)
            ).all()
        }
        if (preferred | reserved) - valid:
            raise HTTPException(status_code=422, detail="Unknown table in the list.")

        for row in db.exec(
            select(VenueNightTable)
            .where(VenueNightTable.club_id == club_id)
            .where(VenueNightTable.club_night_id == night.id)
        ).all():
            db.delete(row)
        for table_id in sorted(preferred):
            db.add(VenueNightTable(
                club_id=club_id, club_night_id=night.id,
                table_id=table_id, reserved=table_id in reserved,
            ))
        db.commit()

    # Anything about this night that could invalidate a plan already made:
    # tables taken off it, or a schedule change that moves it off a date it was
    # laid out for. Un-holding a table a game was sitting on used to leave the
    # game there AND offer the table to the public — two parties with a claim on
    # one board until somebody noticed.
    try:
        from venue_seating import resync_night

        resync_night(db, club_id, night)
    except Exception:
        pass

    return get_club_nights(ctx=ctx, db=db)


@router.delete("/admin/club-nights/{night_id}")
def delete_club_night(
    night_id: int,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Remove a venue-only night.

    A Call to Arms night can't be deleted here — it exists because the club
    runs that game, and taking it off the venue's diary wouldn't stop the
    players arriving. Its game admin turns the system off; this screen only
    ever owned the table plan.
    """
    _, club_id = ctx
    night = db.get(VenueClubNight, night_id)
    if night is None or night.club_id != club_id:
        raise HTTPException(status_code=404, detail="Club night not found.")
    if night.system_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This night comes from a game system your club runs. Turn the system "
                   "off in Admin if it's finished; removing it here wouldn't stop anyone turning up.",
        )
    for row in db.exec(
        select(VenueNightTable)
        .where(VenueNightTable.club_night_id == night_id)
    ).all():
        db.delete(row)
    db.delete(night)
    db.commit()
    return {"ok": True}


# ---- the venue's own public details ----
#
# blurb / website / Discord invite used to live on the Admin tab's club-page
# form. They're venue facts, edited by whoever runs the venue, so they moved
# here — the same reasoning that keeps the table plan out of ClubSystem.

@router.get("/admin/venue-profile")
def get_venue_profile(ctx=Depends(_require_venue_admin), db: Session = Depends(get_session)):
    from database import resolve_webhook_url
    _, club_id = ctx
    club = db.get(Club, club_id)
    url = resolve_webhook_url(db, club_id, V.WEBHOOK_TYPE_VENUE, None)
    return {
        "name": club.name,
        "blurb": club.blurb,
        "website_url": club.website_url,
        "discord_url": club.discord_url,
        # The venue's ONE set of hours: what the public club page shows and what
        # the availability engine offers slots within.
        "opening_hours": V.opening_hours(db, club_id),
        # Never the whole URL back — a webhook URL is a credential. The last
        # four characters are enough for "is this the one I think it is",
        # matching how the admin tab masks its webhooks.
        "webhook": {"configured": True, "last_four": "…" + url[-4:]} if url
                   else {"configured": False},
    }


class VenueProfileBody(BaseModel):
    blurb: Optional[str] = None
    website_url: Optional[str] = None
    discord_url: Optional[str] = None
    opening_hours: Optional[list] = None


@router.post("/admin/venue-profile")
def save_venue_profile(
    body: VenueProfileBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    _, club_id = ctx
    club = db.get(Club, club_id)
    for field in ("blurb", "website_url", "discord_url"):
        val = getattr(body, field)
        if val is not None:
            setattr(club, field, val.strip() or None)

    if body.opening_hours is not None:
        # Stored as only the OPEN days: the club page treats a missing day as
        # closed, and that's the shape it has always read.
        club.opening_hours = [
            {k: v for k, v in row.items() if k != "closed"}
            for row in _clean_hours(body.opening_hours)
            if not row["closed"]
        ]

    db.add(club)
    db.commit()
    return get_venue_profile(ctx=ctx, db=db)


class WebhookBody(BaseModel):
    url: str


@router.post("/admin/webhook")
def save_venue_webhook(
    body: WebhookBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Set the Discord webhook that booking notifications post to.

    Configured here rather than sending venue staff to Admin → Discord: it's
    the venue's own channel, and the person who ticks "tell me on Discord" is
    the person who should be able to say where.
    """
    from models import ClubWebhook

    url = body.url.strip()
    if not url.startswith("https://discord.com/api/webhooks/") and \
       not url.startswith("https://discordapp.com/api/webhooks/"):
        raise HTTPException(
            status_code=422,
            detail="That doesn't look like a Discord webhook URL. It should start "
                   "https://discord.com/api/webhooks/",
        )
    _, club_id = ctx
    row = db.exec(
        select(ClubWebhook)
        .where(ClubWebhook.club_id == club_id)
        .where(ClubWebhook.webhook_type == V.WEBHOOK_TYPE_VENUE)
        .where(ClubWebhook.system_id.is_(None))
    ).first()
    if row is None:
        row = ClubWebhook(club_id=club_id, webhook_type=V.WEBHOOK_TYPE_VENUE,
                          system_id=None, url=url)
    else:
        row.url = url
    db.add(row)
    db.commit()
    return get_venue_profile(ctx=ctx, db=db)


@router.delete("/admin/webhook")
def delete_venue_webhook(
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    from models import ClubWebhook
    _, club_id = ctx
    for row in db.exec(
        select(ClubWebhook)
        .where(ClubWebhook.club_id == club_id)
        .where(ClubWebhook.webhook_type == V.WEBHOOK_TYPE_VENUE)
        .where(ClubWebhook.system_id.is_(None))
    ).all():
        db.delete(row)
    db.commit()
    return get_venue_profile(ctx=ctx, db=db)


@router.post("/admin/webhook/test")
def test_venue_webhook(
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Post a test message, so a venue finds out the webhook is wrong now rather
    than the first time a booking goes unnoticed."""
    import httpx

    from database import resolve_webhook_url
    _, club_id = ctx
    url = resolve_webhook_url(db, club_id, V.WEBHOOK_TYPE_VENUE, None)
    if not url:
        raise HTTPException(status_code=404, detail="No webhook saved yet.")
    club = db.get(Club, club_id)
    try:
        resp = httpx.post(
            url,
            json={"content": f"✅ Booking notifications for **{club.name}** are working.",
                  "allowed_mentions": {"parse": []}},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Discord rejected that webhook ({resp.status_code}). "
                       f"It may have been deleted — paste a fresh one.",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't reach Discord just now.")
    return {"ok": True}


# ---- events ----
#
# An event holds its tables as ordinary bookings (see VenueEvent), so nothing
# below teaches the availability engine anything new about blocking.

@router.get("/admin/events")
def list_events(
    date_str: Optional[str] = Query(None, alias="date"),
    # ge=0 so days=0 means "just this date" — the day panel asks for exactly
    # one day, and rejecting that made its event list silently always empty.
    days: int = Query(60, ge=0, le=365),
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    from models import VenueEvent
    _, club_id = ctx
    first = date.fromisoformat(date_str) if date_str else V.club_now(db, club_id).date()
    rows = db.exec(
        select(VenueEvent)
        .where(VenueEvent.club_id == club_id)
        .where(VenueEvent.event_date >= first)
        .where(VenueEvent.event_date <= first + timedelta(days=days))
        .order_by(VenueEvent.event_date, VenueEvent.start_time)
    ).all()
    return [_event_payload(db, e) for e in rows]


def _event_payload(db: Session, event) -> dict:
    held = db.exec(
        select(VenueBooking)
        .where(VenueBooking.event_id == event.id)
        .where(VenueBooking.status.in_(V.BLOCKING_STATUSES))
    ).all()
    tables = [db.get(VenueTable, b.table_id) for b in held]
    return {
        "id": event.id,
        "name": event.name,
        "status": event.status,
        "rejection_reason": event.rejection_reason,
        "description": event.description,
        "date": event.event_date.isoformat(),
        "start_time": event.start_time,
        "end_time": event.end_time,
        "tables_needed": event.tables_needed,
        "tables_held": len(held),
        "table_names": sorted(t.name for t in tables if t),
        "public": event.public,
        # Says so plainly rather than letting a half-filled event look booked.
        # An event created when the room was busy can hold fewer tables than it
        # asked for, and the venue needs to know that now, not on the night.
        "short_by": max(0, event.tables_needed - len(held)),
    }


def _seated_tables_on(db: Session, club_id: int, day: date) -> dict:
    """table_id -> the club-night game sitting on it that day, if any."""
    try:
        from venue_seating import tonight

        return tonight(db, club_id, day)["seated"]
    except Exception:
        return {}


def _hold_event_tables(db: Session, event, user: User) -> list[str]:
    """Put this event's tables aside, as ordinary bookings carrying its id.

    for_public=False: an event may take a table held for a club night. Staff
    know what they're doing to their own room.

    But it takes the EMPTY ones first. A held table with a game already seated
    on it is the last thing an event should swallow, and taking one silently is
    how a pair of players arrive to find a tournament on their board. Any game
    that does get displaced is named in the return value and moved by the
    re-lay below.
    """
    start, end = V.to_minutes(event.start_time), V.to_minutes(event.end_time)
    free = V.free_tables_for(
        db, event.club_id, event.event_date, start, end, for_public=False,
    )
    seated = _seated_tables_on(db, event.club_id, event.event_date)
    free.sort(key=lambda t: t.id in seated)          # empty tables first

    displaced: list[str] = []
    for table in free[:event.tables_needed]:
        g = seated.get(table.id)
        if g:
            displaced.append(f'{g["a"]} v {g["b"]} ({table.name})')
        db.add(VenueBooking(
            club_id=event.club_id, table_id=table.id, booking_date=event.event_date,
            start_time=event.start_time, end_time=event.end_time,
            party_size=1, user_id=user.id, contact_name=event.name,
            notes=event.description, status="confirmed",
            created_by_staff=True, event_id=event.id,
        ))
    db.commit()

    if displaced:
        try:
            from venue_seating import resync_all_nights

            resync_all_nights(db, event.club_id)
        except Exception:
            pass
    return displaced


class EventBody(BaseModel):
    name: str
    description: Optional[str] = None
    date: str
    start_time: str
    end_time: str
    tables_needed: int = 1
    public: bool = True


@router.post("/admin/events")
def create_event(
    body: EventBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Create an event and hold its tables.

    Takes whatever it can get and reports the shortfall rather than failing
    outright: a venue told "no" at 5pm on a busy Saturday has learned nothing
    it can act on, while "held 4 of the 6 you wanted" tells them exactly which
    problem they have.
    """
    from models import VenueEvent

    user, club_id = ctx
    try:
        day = date.fromisoformat(body.date)
        start = V.to_minutes(body.start_time)
        end = V.to_minutes(body.end_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Bad date or time.")
    if end <= start:
        raise HTTPException(status_code=422, detail="The event must end after it starts.")
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Give the event a name.")
    if not (1 <= body.tables_needed <= 200):
        raise HTTPException(status_code=422, detail="Tables needed must be 1–200.")

    # An owner approving their own event would be theatre — they are the
    # approver, and the click adds nothing but a step.
    owner = _is_venue_owner(user, club_id)
    event = VenueEvent(
        club_id=club_id, name=body.name.strip(),
        description=(body.description or "").strip() or None,
        event_date=day, start_time=V.to_hhmm(start), end_time=V.to_hhmm(end),
        tables_needed=body.tables_needed, public=body.public,
        created_by_user_id=user.id,
        status="approved" if owner else "pending",
        approved_by_user_id=user.id if owner else None,
        approved_at=datetime.utcnow() if owner else None,
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    displaced = _hold_event_tables(db, event, user)
    # Named, not silent: a game moved off its table is something staff have to
    # know at the moment they cause it, not on the night.
    return {**_event_payload(db, event), "displaced": displaced}


@router.patch("/admin/events/{event_id}")
def patch_event(
    event_id: int,
    body: EventBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Edit an event. Changing the date, time or table count re-holds its
    tables from scratch — releasing first, so an event that shrinks or moves
    gives its old tables back to the public instead of sitting on them."""
    from models import VenueEvent

    user, club_id = ctx
    event = db.get(VenueEvent, event_id)
    if event is None or event.club_id != club_id:
        raise HTTPException(status_code=404, detail="Event not found.")
    try:
        day = date.fromisoformat(body.date)
        start = V.to_minutes(body.start_time)
        end = V.to_minutes(body.end_time)
    except ValueError:
        raise HTTPException(status_code=422, detail="Bad date or time.")
    if end <= start:
        raise HTTPException(status_code=422, detail="The event must end after it starts.")

    moved = (day != event.event_date or V.to_hhmm(start) != event.start_time
             or V.to_hhmm(end) != event.end_time
             or body.tables_needed != event.tables_needed)

    # Moving an approved event, or growing what it takes, re-opens the question
    # that was approved. Letting a bar manager edit a signed-off event into
    # twice the room on a different night would make approval decorative. An
    # owner's edit re-approves itself, for the same reason their creation does.
    if moved and event.status == "approved" and not _is_venue_owner(user, club_id):
        event.status = "pending"
        event.approved_by_user_id = None
        event.approved_at = None

    event.name = body.name.strip() or event.name
    event.description = (body.description or "").strip() or None
    event.event_date, event.start_time, event.end_time = day, V.to_hhmm(start), V.to_hhmm(end)
    event.tables_needed = body.tables_needed
    event.public = body.public
    event.updated_at = datetime.utcnow()
    db.add(event)

    if moved:
        for b in db.exec(
            select(VenueBooking).where(VenueBooking.event_id == event.id)
        ).all():
            db.delete(b)
        db.commit()
        # A rejected event holds nothing; re-holding here would quietly give it
        # the room back through the edit form.
        if event.status != "rejected":
            _hold_event_tables(db, event, user)
    db.commit()
    db.refresh(event)
    return _event_payload(db, event)


@router.delete("/admin/events/{event_id}")
def delete_event(
    event_id: int,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Cancel an event and release every table it was holding."""
    from models import VenueEvent
    _, club_id = ctx
    event = db.get(VenueEvent, event_id)
    if event is None or event.club_id != club_id:
        raise HTTPException(status_code=404, detail="Event not found.")
    for b in db.exec(select(VenueBooking).where(VenueBooking.event_id == event_id)).all():
        db.delete(b)
    db.delete(event)
    db.commit()
    return {"ok": True}


class EventDecisionBody(BaseModel):
    reason: Optional[str] = None


@router.post("/admin/events/{event_id}/approve")
def approve_event(
    event_id: int,
    ctx=Depends(_require_venue_owner),
    db: Session = Depends(get_session),
):
    """Sign off an event. Owner only — the whole point of the gate."""
    from models import VenueEvent
    user, club_id = ctx
    event = db.get(VenueEvent, event_id)
    if event is None or event.club_id != club_id:
        raise HTTPException(status_code=404, detail="Event not found.")
    if event.status == "approved":
        return _event_payload(db, event)

    # A rejected event released its tables. Approving it later has to take them
    # back, and the room may have filled in the meantime — hence the shortfall
    # in the payload rather than a silent half-booked event.
    if event.status == "rejected":
        _hold_event_tables(db, event, user)

    event.status = "approved"
    event.rejection_reason = None
    event.approved_by_user_id = user.id
    event.approved_at = datetime.utcnow()
    event.updated_at = datetime.utcnow()
    db.add(event)
    db.commit()
    db.refresh(event)
    return _event_payload(db, event)


@router.post("/admin/events/{event_id}/reject")
def reject_event(
    event_id: int,
    body: EventDecisionBody,
    ctx=Depends(_require_venue_owner),
    db: Session = Depends(get_session),
):
    """Turn an event down and release its tables straight away.

    The event row stays, carrying the reason. Deleting it would lose the fact
    that someone asked and was told no, which is exactly what the person who
    proposed it needs to see.
    """
    from models import VenueEvent
    _, club_id = ctx
    event = db.get(VenueEvent, event_id)
    if event is None or event.club_id != club_id:
        raise HTTPException(status_code=404, detail="Event not found.")

    for b in db.exec(select(VenueBooking).where(VenueBooking.event_id == event_id)).all():
        db.delete(b)
    event.status = "rejected"
    event.rejection_reason = (body.reason or "").strip() or None
    event.approved_by_user_id = None
    event.approved_at = None
    event.updated_at = datetime.utcnow()
    db.add(event)
    db.commit()
    db.refresh(event)
    return _event_payload(db, event)


# ---- the floor plan ----

@router.get("/admin/layout")
def get_layout(ctx=Depends(_require_venue_admin), db: Session = Depends(get_session)):
    """The venue's plan. Creates a first room and lays existing tables out in
    it, so nobody is met with an empty canvas and a filing job."""
    _, club_id = ctx
    V.ensure_default_room(db, club_id)
    return V.layout(db, club_id)


class RoomBody(BaseModel):
    id: Optional[int] = None
    name: str = "Room"
    width_ft: float = 30.0
    depth_ft: float = 20.0
    notes: Optional[str] = None


@router.post("/admin/layout/rooms")
def save_room(
    body: RoomBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    from models import VenueRoom
    _, club_id = ctx
    if not (4 <= body.width_ft <= 400 and 4 <= body.depth_ft <= 400):
        raise HTTPException(status_code=422, detail="Rooms run from 4 to 400 feet a side.")
    if body.id is None:
        room = VenueRoom(club_id=club_id, sort_order=len(V.rooms_for(db, club_id)))
    else:
        room = db.get(VenueRoom, body.id)
        if room is None or room.club_id != club_id:
            raise HTTPException(status_code=404, detail="Room not found.")
    room.name = body.name.strip() or room.name
    room.width_ft, room.depth_ft = body.width_ft, body.depth_ft
    room.notes = (body.notes or "").strip() or None
    db.add(room)
    db.commit()
    return V.layout(db, club_id)


@router.delete("/admin/layout/rooms/{room_id}")
def delete_room(
    room_id: int,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Remove a room. Its tables are unplaced rather than deleted — the room
    closing doesn't mean the tables stopped existing, and they carry bookings
    and history that must not vanish with a wall."""
    from models import VenueFeature, VenueRoom
    _, club_id = ctx
    room = db.get(VenueRoom, room_id)
    if room is None or room.club_id != club_id:
        raise HTTPException(status_code=404, detail="Room not found.")
    if len(V.rooms_for(db, club_id)) <= 1:
        raise HTTPException(status_code=409, detail="A venue needs at least one room.")

    moved = 0
    for t in db.exec(select(VenueTable).where(VenueTable.room_id == room_id)).all():
        t.room_id, t.pos_x, t.pos_y = None, None, None
        db.add(t)
        moved += 1
    for f in db.exec(select(VenueFeature).where(VenueFeature.room_id == room_id)).all():
        db.delete(f)
    db.delete(room)
    db.commit()
    out = V.layout(db, club_id)
    out["unplaced"] = moved
    return out


class PlanTable(BaseModel):
    id: Optional[int] = None
    name: str
    shape: str = "rect"
    color: str = "slate"
    room_id: Optional[int] = None
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None
    width_ft: float = 6.0
    depth_ft: float = 4.0
    rotation: float = 0.0
    seats: int = 2
    active: bool = True
    notes: Optional[str] = None


class PlanFeature(BaseModel):
    id: Optional[int] = None
    room_id: int
    kind: str = "wall"
    shape: str = "rect"
    color: str = "grey"
    label: Optional[str] = None
    pos_x: float = 0.0
    pos_y: float = 0.0
    width_ft: float = 4.0
    depth_ft: float = 2.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False


class LayoutBody(BaseModel):
    tables: list[PlanTable] = []
    features: list[PlanFeature] = []
    deleted_table_ids: list[int] = []
    deleted_feature_ids: list[int] = []


@router.post("/admin/layout")
def save_layout(
    body: LayoutBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Save the whole plan in one go.

    One request rather than a call per drag, because a floor plan is edited as
    a single act — nudge three tables, turn one, rename another — and a save per
    interaction would mean a half-applied layout whenever the network blinked.

    Deletion still refuses a table with bookings against it, exactly as the old
    list did: the plan is a nicer way to edit the same inventory, not a way
    round the rule that protects its history.
    """
    from models import VenueFeature, VenueRoom
    _, club_id = ctx

    valid_rooms = {r.id for r in V.rooms_for(db, club_id)}

    for tid in body.deleted_table_ids:
        t = db.get(VenueTable, tid)
        if t is None or t.club_id != club_id:
            continue
        used = db.exec(select(VenueBooking).where(VenueBooking.table_id == tid)).first()
        if used is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{t.name} has bookings against it. Turn it off instead of deleting "
                       f"it — that stops new bookings and keeps the old ones readable.",
            )
        for row in db.exec(
            select(VenueNightTable).where(VenueNightTable.table_id == tid)
        ).all():
            db.delete(row)
        db.delete(t)

    for fid in body.deleted_feature_ids:
        f = db.get(VenueFeature, fid)
        if f is not None and f.club_id == club_id:
            db.delete(f)

    for i, row in enumerate(body.tables):
        if row.room_id is not None and row.room_id not in valid_rooms:
            raise HTTPException(status_code=422, detail="Unknown room.")
        if not (1 <= row.width_ft <= 100 and 1 <= row.depth_ft <= 100):
            raise HTTPException(status_code=422, detail="Tables run from 1 to 100 feet a side.")
        if row.id is None:
            t = VenueTable(club_id=club_id)
        else:
            t = db.get(VenueTable, row.id)
            if t is None or t.club_id != club_id:
                continue
        t.name = row.name.strip() or t.name or "Table"
        t.shape = row.shape if row.shape in ("rect", "round", "oval") else "rect"
        t.color = row.color if row.color in V.TABLE_COLORS else "slate"
        t.room_id = row.room_id
        t.pos_x, t.pos_y = row.pos_x, row.pos_y
        t.width_ft, t.depth_ft = row.width_ft, row.depth_ft
        # Kept in [0, 360) so "is it turned?" is one comparison and the UI never
        # shows a table at -90 degrees.
        t.rotation = float(row.rotation) % 360
        t.seats = max(1, row.seats)
        t.active = row.active
        t.notes = (row.notes or "").strip() or None
        t.sort_order = i
        # Derived, never typed: the label and the shape can't disagree.
        t.size_label = V.size_label_for(row.width_ft, row.depth_ft)
        db.add(t)

    for row in body.features:
        if row.room_id not in valid_rooms:
            raise HTTPException(status_code=422, detail="Unknown room.")
        if row.id is None:
            f = VenueFeature(club_id=club_id, room_id=row.room_id)
        else:
            f = db.get(VenueFeature, row.id)
            if f is None or f.club_id != club_id:
                continue
        f.room_id = row.room_id
        if row.kind not in V.FEATURE_KINDS:
            raise HTTPException(status_code=422, detail=f"Unknown fixture: {row.kind}")
        f.kind = row.kind
        f.shape = row.shape if row.shape in ("rect", "round", "oval") else "rect"
        f.color = row.color if row.color in V.TABLE_COLORS else "grey"
        f.label = (row.label or "").strip() or None
        f.pos_x, f.pos_y = row.pos_x, row.pos_y
        f.width_ft, f.depth_ft = row.width_ft, row.depth_ft
        f.rotation = float(row.rotation) % 360
        f.flip_h = bool(row.flip_h)
        f.flip_v = bool(row.flip_v)
        db.add(f)

    db.commit()
    # Saving the plan can retire a table a game was seated on. See resync_night.
    try:
        from venue_seating import resync_all_nights

        resync_all_nights(db, club_id)
    except Exception:
        pass
    return V.layout(db, club_id)


@router.get("/admin/layout/occupancy")
def layout_occupancy(
    date_str: str = Query(..., alias="date"),
    at: Optional[str] = None,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """What every table is doing on a date — the plan as tonight's view."""
    _, club_id = ctx
    try:
        day = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.")
    return V.occupancy(db, club_id, day, at)


# ---------------------------------------------------------------------------
# Seating: where the pairings meet the room. See venue_seating.py.
# ---------------------------------------------------------------------------

def _night_or_404(db: Session, club_id: int, night_id: int) -> VenueClubNight:
    night = db.get(VenueClubNight, night_id)
    if night is None or night.club_id != club_id:
        raise HTTPException(status_code=404, detail="Club night not found.")
    return night


def _day_or_422(date_str: str) -> date:
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD.")


@router.get("/admin/seating")
def get_seating(
    night_id: int,
    date_str: str = Query(..., alias="date"),
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """One club night's table plan for one date: what tonight needs, where each
    game is sitting, and which held tables are going spare."""
    import venue_seating as S

    _, club_id = ctx
    night = _night_or_404(db, club_id, night_id)
    return S.view(db, club_id, night, _day_or_422(date_str))


class SeatingBody(BaseModel):
    night_id: int
    date: str


@router.post("/admin/seating/generate")
def generate_seating(
    body: SeatingBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Lay tonight's games out on tonight's tables.

    Safe to run again: locked seats stay put and everything already placed
    keeps its table, so a late signup adds one game rather than reshuffling the
    room. See venue_seating.generate.
    """
    import venue_seating as S

    _, club_id = ctx
    night = _night_or_404(db, club_id, body.night_id)
    day = _day_or_422(body.date)

    result = S.generate(db, club_id, night, day)
    if not result["ok"]:
        raise HTTPException(
            status_code=422,
            detail="This night doesn't run through Call to Arms, so there are no pairings "
                   "to lay out. Hold its tables by hand on the Club nights tab.",
        )
    return S.view(db, club_id, night, day)


class SeatMoveBody(BaseModel):
    night_id: int
    date: str
    pairing_id: int
    # None un-seats the game: it stays on the list with no table, which is how
    # staff say "this one is playing on the floor" without deleting anything.
    table_id: Optional[int] = None


@router.post("/admin/seating/move")
def move_seat(
    body: SeatMoveBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Put one game on a specific table.

    Marks the seat LOCKED, because a human chose it — regenerating must not
    quietly undo a decision someone made while standing in the room.
    """
    import venue_seating as S

    _, club_id = ctx
    night = _night_or_404(db, club_id, body.night_id)
    day = _day_or_422(body.date)

    seating = S.get_seating(db, club_id, night.id, day)
    if seating is None:
        raise HTTPException(status_code=404, detail="Lay the tables out first.")

    seats = {s.pairing_id: s for s in S.seats_for(db, seating.id)}
    seat = seats.get(body.pairing_id)

    if body.table_id is None:
        if seat is not None:
            db.delete(seat)
            db.commit()
        return S.view(db, club_id, night, day)

    table = db.get(VenueTable, body.table_id)
    if table is None or table.club_id != club_id or not table.active:
        raise HTTPException(status_code=404, detail="Table not found.")

    # Two games on one table is the one thing this screen must never allow.
    # Swap rather than refuse: staff dragging game A onto game B's table mean
    # "these two switch", and making them clear one first is busywork.
    other = next((s for s in seats.values()
                  if s.table_id == table.id and s.pairing_id != body.pairing_id), None)
    if other is not None:
        if seat is None:
            db.delete(other)
        else:
            other.table_id, seat.table_id = seat.table_id, table.id
            other.locked = True
            seat.locked = True
            db.add(other)
            db.add(seat)
            db.commit()
            return S.view(db, club_id, night, day)

    if seat is None:
        seat = VenueSeat(club_id=club_id, seating_id=seating.id,
                         pairing_id=body.pairing_id, table_id=table.id, locked=True)
    else:
        seat.table_id = table.id
        seat.locked = True
    db.add(seat)
    db.commit()
    return S.view(db, club_id, night, day)


class ReleaseBody(BaseModel):
    night_id: int
    date: str
    released: bool = True


@router.post("/admin/seating/release")
def release_spare(
    body: ReleaseBody,
    ctx=Depends(_require_venue_admin),
    db: Session = Depends(get_session),
):
    """Hand this night's spare tables back to the public, or take them back.

    Deliberately a decision rather than a calculation — see VenueSeating's
    docstring. Taking them back can't cancel a booking someone has already
    made on one, so the response says how many are still genuinely free.
    """
    import venue_seating as S

    _, club_id = ctx
    night = _night_or_404(db, club_id, body.night_id)
    day = _day_or_422(body.date)

    seating = S.get_seating(db, club_id, night.id, day)
    if seating is None:
        raise HTTPException(status_code=404, detail="Lay the tables out first.")

    seating.released = bool(body.released)
    db.add(seating)
    db.commit()

    view = S.view(db, club_id, night, day)
    if not body.released:
        booked = {b.table_id for b in V.bookings_on(db, club_id, day)}
        view["taken_back_but_booked"] = sorted(set(view["spare_table_ids"]) & booked)
    return view
