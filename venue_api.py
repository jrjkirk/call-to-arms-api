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
    VenueConfig, VenueNightTable, VenueStaff, VenueTable, Player,
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


def _require_venue_owner(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
) -> tuple[User, int]:
    """Stricter than _require_venue_admin: the club's own super-admin, or a
    platform admin. Granting venue access is an ownership decision, not a
    day-to-day one — the bar manager runs the diary but doesn't get to hand out
    keys, and shouldn't be able to promote themselves' colleagues either."""
    if user.is_platform_admin or (user.is_super_admin and user.club_id == club_id):
        return user, club_id
    raise HTTPException(
        status_code=403,
        detail="Only a club super-admin can change who has venue access.",
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
        "booking_hours": cfg.booking_hours or V.DEFAULT_BOOKING_HOURS,
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
    window = V.hours_for(cfg, day)
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
        "booking_hours": cfg.booking_hours or V.DEFAULT_BOOKING_HOURS,
        "notify_email": cfg.notify_email,
        "notify_emails": cfg.notify_emails or [],
        "notify_discord": cfg.notify_discord,
        "booking_blurb": cfg.booking_blurb,
        "confirmation_note": cfg.confirmation_note,
        "promote_club_nights": cfg.promote_club_nights,
        # So the settings screen can warn that email is on but points nowhere,
        # rather than letting the venue find out when a booking goes unseen.
        "effective_emails": V.staff_emails(db, club_id, cfg),
        "club_contact_email": club.contact_email if club else None,
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
    booking_hours: Optional[list] = None
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

    if body.booking_hours is not None:
        cfg.booking_hours = _clean_hours(body.booking_hours)

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
        if row.get("closed"):
            by_day[day] = {"day": day, "open": None, "close": None, "closed": True}
            continue
        try:
            o, c = V.to_minutes(row.get("open")), V.to_minutes(row.get("close"))
        except (ValueError, TypeError):
            by_day[day] = {"day": day, "open": None, "close": None, "closed": True}
            continue
        if c <= o:
            raise HTTPException(
                status_code=422,
                detail=f"{day}: closing time must be after opening time.",
            )
        by_day[day] = {"day": day, "open": V.to_hhmm(o), "close": V.to_hhmm(c), "closed": False}
    return [by_day.get(d, {"day": d, "open": None, "close": None, "closed": True})
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
    rows = db.exec(
        select(VenueTable)
        .where(VenueTable.club_id == club_id)
        .order_by(VenueTable.sort_order, VenueTable.id)
    ).all()
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
    tables = db.exec(
        select(VenueTable)
        .where(VenueTable.club_id == club_id)
        .order_by(VenueTable.sort_order, VenueTable.id)
    ).all()

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
             "start_time": b.start_time,
             "end_time": b.end_time,
             "created_by_staff": b.created_by_staff,
             "staff_note": b.staff_note}
            for b in rows
        ],
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
    _, club_id = ctx
    first = V.club_now(db, club_id).date()
    pending = db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.status == "requested")
        .where(VenueBooking.booking_date >= first)
        .order_by(VenueBooking.booking_date, VenueBooking.start_time)
    ).all()
    return {
        "days": [V.day_overview(db, club_id, first + timedelta(days=i)) for i in range(days)],
        "pending": [V.describe_booking(db, b) for b in pending],
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
    if body.table_id is not None:
        chosen = next((t for t in free if t.id == body.table_id), None)
        if chosen is None:
            raise HTTPException(status_code=409, detail="That table isn't free for that slot.")
    else:
        candidates = [t for t in free if t.seats >= body.party_size] or free
        if not candidates:
            raise HTTPException(status_code=409, detail="No table free for that slot.")
        chosen = min(candidates, key=lambda t: (t.seats, t.sort_order, t.id))

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
    # No notification: staff entered this themselves and don't need telling.
    return {"ok": True, "booking": V.describe_booking(db, b)}


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

    tables = db.exec(
        select(VenueTable)
        .where(VenueTable.club_id == club_id)
        .order_by(VenueTable.sort_order, VenueTable.id)
    ).all()
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
            if body.session_cadence not in ("weekly", "fortnightly"):
                raise HTTPException(status_code=422, detail="Cadence must be weekly or fortnightly.")
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
        if (night.session_cadence or "weekly") == "fortnightly" and night.cadence_anchor is None:
            raise HTTPException(
                status_code=422,
                detail="A fortnightly night needs a date it last ran, so we know which weeks it falls on.",
            )

    if body.expected_tables is not None and not (0 <= body.expected_tables <= 200):
        raise HTTPException(status_code=422, detail="Expected tables must be 0–200.")
    night.expected_tables = body.expected_tables
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
