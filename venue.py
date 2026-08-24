"""Venue management: booking policy, the availability engine, and how staff
hear about a booking.

The club IS the venue here (see the models.py section header). Everything is
club-scoped; there is no venue id to pass around.

Two things in this module are worth reading before changing anything:

`availability()` is the only place that decides whether a table is free. Every
caller — the public grid, the booking endpoint, the staff console — goes
through it, so a rule added here applies everywhere and a booking can never be
accepted on terms the grid didn't offer.

Times are local "HH:MM" strings throughout, resolved against the club's
timezone, matching ClubEvent.start_time and TableBookingConfig.cutoff_time.
They are only converted to real datetimes at the edges (lead-time checks,
notification rendering). Minutes-since-midnight is the working unit inside.
"""
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from models import (
    Club, ClubSystem, SystemConfig, User, VenueBooking, VenueConfig,
    VenueStaff, VenueTable,
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday")

# A booking in one of these states occupies its table. Anything else has
# released it, so the slot is bookable again.
BLOCKING_STATUSES = ("requested", "confirmed")

DEFAULT_BOOKING_HOURS = [
    {"day": d, "open": "17:00", "close": "23:00", "closed": d in ("Monday", "Tuesday")}
    for d in WEEKDAYS
]


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def can_admin_venue(db: Session, user: Optional[User], club_id: int) -> bool:
    """Whether this user may see Venue Admin at this club.

    Three ways in, in order of how common they are:
      - a VenueStaff row at this club (the bar manager, who is otherwise a
        plain player and holds no system scopes at all)
      - the club's own super-admin, at their own club only
      - a platform admin, anywhere

    Mirrors admin_scopes' rule that super-admin is a home-club power: a
    super-admin of one club is a plain punter at every other club, and must not
    inherit the run of someone else's bar.
    """
    if user is None:
        return False
    if user.is_platform_admin:
        return True
    if user.is_super_admin and user.club_id == club_id:
        return True
    row = db.exec(
        select(VenueStaff).where(
            VenueStaff.user_id == user.id, VenueStaff.club_id == club_id
        )
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(db: Session, club_id: int) -> Optional[VenueConfig]:
    return db.exec(select(VenueConfig).where(VenueConfig.club_id == club_id)).first()


def get_or_create_config(db: Session, club_id: int) -> VenueConfig:
    """Read-or-seed. Created disabled with sensible evening hours, so opening
    Venue Admin for the first time shows a filled-in form to adjust rather than
    an empty one to invent — and nothing is public until `enabled` is set."""
    cfg = get_config(db, club_id)
    if cfg is not None:
        return cfg
    cfg = VenueConfig(club_id=club_id, booking_hours=DEFAULT_BOOKING_HOURS)
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def to_minutes(hhmm: str) -> int:
    """"HH:MM" -> minutes since midnight. Raises ValueError on anything else,
    deliberately: a malformed time must fail loudly at the edge rather than
    silently becoming midnight and booking a table at 00:00."""
    parts = (hhmm or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Bad time {hhmm!r}, expected HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Bad time {hhmm!r}")
    return h * 60 + m


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def club_now(db: Session, club_id: int) -> datetime:
    """Now, in the club's own timezone. Every "is this in the past" and
    lead-time question has to be asked locally: the server runs UTC, and for
    half the year a London venue's 23:00 close is 22:00 UTC."""
    club = db.get(Club, club_id)
    tz = ZoneInfo(club.timezone if club and club.timezone else "Europe/London")
    return datetime.now(tz)


def hours_for(cfg: VenueConfig, day: date) -> Optional[tuple[int, int]]:
    """(open, close) in minutes for a given date, or None if not bookable."""
    rows = cfg.booking_hours or DEFAULT_BOOKING_HOURS
    name = WEEKDAYS[day.weekday()]
    for row in rows:
        if (row.get("day") or "") == name:
            if row.get("closed"):
                return None
            try:
                return to_minutes(row.get("open")), to_minutes(row.get("close"))
            except (ValueError, TypeError):
                return None
    return None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open intervals: a booking ending at 20:00 and one starting at 20:00
    do not overlap. Without this a table would sit idle between back-to-back
    games for no reason."""
    return a_start < b_end and b_start < a_end


def bookings_on(db: Session, club_id: int, day: date) -> list[VenueBooking]:
    return db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.booking_date == day)
        .where(VenueBooking.status.in_(BLOCKING_STATUSES))
    ).all()


def active_tables(db: Session, club_id: int) -> list[VenueTable]:
    return db.exec(
        select(VenueTable)
        .where(VenueTable.club_id == club_id, VenueTable.active == True)
        .order_by(VenueTable.sort_order, VenueTable.id)
    ).all()


def free_tables_for(
    db: Session,
    club_id: int,
    day: date,
    start: int,
    end: int,
    exclude_booking_id: Optional[int] = None,
    existing: Optional[list[VenueBooking]] = None,
    tables: Optional[list[VenueTable]] = None,
    party_size: Optional[int] = None,
) -> list[VenueTable]:
    """Tables with nothing on them for [start, end).

    `existing` and `tables` are accepted so the availability grid can load both
    once and reuse them across every slot it tests, instead of two queries per
    slot per table.

    `exclude_booking_id` lets staff move or extend a booking without it
    colliding with itself.
    """
    rows = bookings_on(db, club_id, day) if existing is None else existing
    all_tables = active_tables(db, club_id) if tables is None else tables

    taken: set[int] = set()
    for b in rows:
        if exclude_booking_id is not None and b.id == exclude_booking_id:
            continue
        try:
            if _overlaps(start, end, to_minutes(b.start_time), to_minutes(b.end_time)):
                taken.add(b.table_id)
        except ValueError:
            # A row with an unparseable time is treated as blocking. Better to
            # under-sell one table than to double-book a real one.
            taken.add(b.table_id)

    out = [t for t in all_tables if t.id not in taken]
    if party_size is not None:
        out = [t for t in out if t.seats >= party_size]
    return out


def availability(
    db: Session,
    club_id: int,
    day: date,
    duration_minutes: Optional[int] = None,
    party_size: Optional[int] = None,
) -> dict:
    """Bookable start times for one date.

    The single source of truth on whether a slot may be sold. Returns every
    slot the venue's hours allow, each marked available or not with a reason,
    rather than silently dropping the unavailable ones — the public grid shows
    a full evening with the busy parts greyed out, which reads as a real
    venue's diary instead of a suspiciously short list.
    """
    cfg = get_config(db, club_id)
    if cfg is None or not cfg.enabled:
        return {"date": day.isoformat(), "enabled": False, "slots": [], "tables": 0}

    duration = duration_minutes or cfg.min_duration_minutes
    window = hours_for(cfg, day)
    tables = active_tables(db, club_id)

    base = {
        "date": day.isoformat(),
        "enabled": True,
        "duration_minutes": duration,
        "tables": len(tables),
        "slot_minutes": cfg.slot_minutes,
    }
    if window is None:
        return {**base, "slots": [], "closed": True}
    if not tables:
        return {**base, "slots": [], "no_tables": True}

    open_m, close_m = window
    now = club_now(db, club_id)
    today = now.date()
    cutoff = now.hour * 60 + now.minute + cfg.lead_time_minutes

    existing = bookings_on(db, club_id, day)
    slots = []
    start = open_m
    while start + duration <= close_m:
        end = start + duration
        reason = None
        free = free_tables_for(
            db, club_id, day, start, end,
            existing=existing, tables=tables, party_size=party_size,
        )
        if day < today or (day == today and start < cutoff):
            # Past, or too close to now to warn staff in time.
            reason = "too_soon"
        elif not free:
            reason = "full"

        slots.append({
            "start": to_hhmm(start),
            "end": to_hhmm(end),
            "available": reason is None,
            "reason": reason,
            "tables_free": len(free),
        })
        start += cfg.slot_minutes

    return {**base, "slots": slots}


# ---------------------------------------------------------------------------
# Club nights
#
# The reason this feature is worth building rather than buying. A generic
# booking tool knows what has been booked; this app also knows how many players
# have already said they're coming to Wednesday's Old World night. Staff were
# "constantly using judgment to navigate the club night bookings" precisely
# because those two facts lived in different places. Here they're one view.
# ---------------------------------------------------------------------------

def club_nights_on(db: Session, club_id: int, day: date) -> list[dict]:
    """The club sessions running at this venue on one date, with how many
    players have signed up so far.

    Derived, not stored — recurring sessions come from each ClubSystem's
    session_day/cadence exactly as the club calendar derives them, so a venue
    never has to re-enter its own club nights and they can't drift apart.
    """
    from week_logic import sessions_in_range, _fmt  # local: avoids an import cycle
    from models import Signup

    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
    ).all()

    out = []
    for cs, sc in rows:
        try:
            hits = sessions_in_range(
                cs.session_day, cs.session_cadence, cs.cadence_anchor, day, day
            )
        except (AssertionError, KeyError):
            # A fortnightly system with no anchor, or an unrecognised day name.
            # One misconfigured system must not blank the venue's whole diary.
            continue
        if not hits:
            continue

        week = _fmt(day)
        signups = db.exec(
            select(Signup)
            .where(Signup.club_id == club_id)
            .where(Signup.system == sc.legacy_system_name)
            .where(Signup.week == week)
        ).all()

        out.append({
            "system_id": sc.id,
            "system": sc.name,
            "slug": sc.slug,
            "legacy_system_name": sc.legacy_system_name,
            "accent_color": cs.accent_color,
            "start_time": cs.session_start_time,
            "signups": len(signups),
            # What the club night alone is expected to need. Two players to a
            # table is the same assumption TableBookingConfig defaults to.
            "tables_expected": -(-len(signups) // 2),
        })
    return sorted(out, key=lambda r: (r["start_time"] or "", r["system"]))


def day_overview(db: Session, club_id: int, day: date) -> dict:
    """Everything happening at the venue on one date: public bookings, club
    nights, and how full it is.

    `tables_committed` deliberately adds club-night demand to booked tables.
    A Wednesday with three public bookings and eighteen Old World players
    signed up is not a quiet night, and a view that counted only the three
    would tell staff exactly the wrong thing.
    """
    cfg = get_config(db, club_id)
    tables = active_tables(db, club_id)
    bookings = bookings_on(db, club_id, day)
    nights = club_nights_on(db, club_id, day)

    booked_tables = len({b.table_id for b in bookings})
    night_tables = sum(n["tables_expected"] for n in nights)
    committed = booked_tables + night_tables
    total = len(tables)

    return {
        "date": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()],
        "enabled": bool(cfg and cfg.enabled),
        "tables_total": total,
        "tables_booked": booked_tables,
        "tables_club_night": night_tables,
        "tables_committed": committed,
        # None rather than 0 when there are no tables: "0% full" and "we don't
        # know how many tables you have" are different answers, and a progress
        # bar should show the second as empty state, not as quiet.
        "load": round(committed / total, 3) if total else None,
        "over_capacity": total > 0 and committed > total,
        "club_nights": nights,
        "bookings": len(bookings),
    }


# ---------------------------------------------------------------------------
# Telling staff
#
# Which channel is configuration, not a hardcoded choice: a bar run off a staff
# Discord wants a ping in a channel, a game store with an inbox wants an email,
# and a venue that lives in the console wants neither. All three are valid, so
# notify_email and notify_discord are independent switches and either may be
# off.
#
# EVERYTHING HERE FAILS SOFT. A booking that was accepted must never be undone
# because Resend was slow or a webhook had been revoked — the booking is the
# record, the notification is a courtesy, and the console shows it either way.
# Failures are captured for the alerting channel, not raised.
# ---------------------------------------------------------------------------

WEBHOOK_TYPE_VENUE = "venue_booking"


def staff_emails(db: Session, club_id: int, cfg: VenueConfig) -> list[str]:
    """Where staff notifications go. Falls back to the club's contact address,
    so a venue that has already told us where to write doesn't have to say it
    twice, and turning notifications on can't silently send to nobody."""
    listed = [e.strip() for e in (cfg.notify_emails or []) if (e or "").strip()]
    if listed:
        return listed
    club = db.get(Club, club_id)
    return [club.contact_email] if club and club.contact_email else []


def describe_booking(db: Session, booking: VenueBooking) -> dict:
    """Flat, display-ready view of a booking. One place builds these strings so
    the email, the Discord post, the console and the confirmation page can
    never describe the same booking differently."""
    table = db.get(VenueTable, booking.table_id)
    system = db.get(SystemConfig, booking.system_id) if booking.system_id else None
    game = system.name if system else (booking.game_note or "Not specified")
    return {
        "id": booking.id,
        "date": booking.booking_date.strftime("%A %d %B %Y"),
        "date_iso": booking.booking_date.isoformat(),
        "time": f"{booking.start_time}–{booking.end_time}",
        "table": table.name if table else f"#{booking.table_id}",
        "table_size": table.size_label if table else None,
        "party_size": booking.party_size,
        "game": game,
        "name": booking.contact_name,
        "email": booking.contact_email,
        "phone": booking.contact_phone,
        "notes": booking.notes,
        "status": booking.status,
    }


def _staff_email_html(club: Club, d: dict, needs_action: bool) -> str:
    rows = [
        ("When", f"{d['date']}, {d['time']}"),
        ("Table", d["table"] + (f" ({d['table_size']})" if d["table_size"] else "")),
        ("Playing", d["game"]),
        ("Party", f"{d['party_size']} player{'s' if d['party_size'] != 1 else ''}"),
        ("Booked by", d["name"]),
    ]
    if d["email"]:
        rows.append(("Email", d["email"]))
    if d["phone"]:
        rows.append(("Phone", d["phone"]))
    if d["notes"]:
        rows.append(("Notes", d["notes"]))

    cells = "".join(
        f'<tr><td style="padding:4px 14px 4px 0;color:#666;white-space:nowrap">{k}</td>'
        f'<td style="padding:4px 0"><strong>{v}</strong></td></tr>'
        for k, v in rows
    )
    lead = ("A table has been requested and is waiting for you to confirm it."
            if needs_action else "A table has been booked.")
    return (
        f'<div style="font-family:system-ui,sans-serif;font-size:15px;color:#111">'
        f"<p>{lead}</p>"
        f'<table style="border-collapse:collapse">{cells}</table>'
        f'<p style="color:#666;font-size:13px">{club.name} — sent by Call to Arms.</p>'
        f"</div>"
    )


def _staff_discord_text(club: Club, d: dict, needs_action: bool) -> str:
    head = "📋 **Table requested**" if needs_action else "✅ **Table booked**"
    lines = [
        f"{head} at {club.name}",
        f"🗓️ {d['date']} · ⏰ {d['time']}",
        f"🎲 {d['game']} · 🪑 {d['table']} · 👥 {d['party_size']}",
        f"🙋 {d['name']}",
    ]
    if d["notes"]:
        lines.append(f"📝 {d['notes']}")
    if needs_action:
        lines.append("_Waiting for staff to confirm._")
    return "\n".join(lines)


def notify_staff(db: Session, club_id: int, booking: VenueBooking) -> dict:
    """Tell staff about a booking on whichever channels are switched on.

    Returns what happened per channel so the caller can log it. Never raises.
    """
    import httpx

    from database import resolve_webhook_url
    from observability import capture

    result = {"email": None, "discord": None}
    cfg = get_config(db, club_id)
    club = db.get(Club, club_id)
    if cfg is None or club is None:
        return result

    d = describe_booking(db, booking)
    needs_action = booking.status == "requested"

    if cfg.notify_email:
        recipients = staff_emails(db, club_id, cfg)
        if not recipients:
            result["email"] = "no_recipients"
        else:
            try:
                from emailer import send_email
                verb = "requested" if needs_action else "booked"
                send_email(
                    to=recipients,
                    subject=f"Table {verb}: {d['date']} {d['time']} — {d['name']}",
                    html=_staff_email_html(club, d, needs_action),
                )
                result["email"] = "sent"
            except Exception as e:
                capture(e, kind="venue_booking_email", club_id=club_id)
                result["email"] = "failed"

    if cfg.notify_discord:
        url = resolve_webhook_url(db, club_id, WEBHOOK_TYPE_VENUE, None)
        if not url:
            result["discord"] = "not_configured"
        else:
            try:
                resp = httpx.post(
                    url,
                    json={"content": _staff_discord_text(club, d, needs_action),
                          "allowed_mentions": {"parse": []}},
                    timeout=httpx.Timeout(10.0, connect=5.0),
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"venue webhook returned {resp.status_code}")
                result["discord"] = "sent"
            except Exception as e:
                capture(e, kind="venue_booking_webhook", club_id=club_id)
                result["discord"] = "failed"

    return result


def club_night_pitch(db: Session, club_id: int, booking: VenueBooking) -> Optional[dict]:
    """The cross-sell: a club night at this venue the booker might not know
    about.

    Prefers a night on the very evening they've booked — "you'll be here
    anyway, and so are twenty Old World players" is a far better invitation
    than a generic listing. Falls back to the club's next session otherwise.

    Deliberately skipped when they booked to play that system already: telling
    someone who booked a table for The Old World about the Old World night
    reads as a robot, not a welcome.
    """
    cfg = get_config(db, club_id)
    if cfg is None or not cfg.promote_club_nights:
        return None

    same_night = [
        n for n in club_nights_on(db, club_id, booking.booking_date)
        if n["system_id"] != booking.system_id
    ]
    if same_night:
        night = max(same_night, key=lambda n: n["signups"])
        return {**night, "same_evening": True, "date": booking.booking_date.isoformat()}

    from week_logic import next_session_date

    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
    ).all()
    best = None
    for cs, sc in rows:
        if sc.id == booking.system_id:
            continue
        try:
            nxt = next_session_date(
                cs.session_day, cs.session_cadence, cs.cadence_anchor,
                booking.booking_date,
            )
        except (AssertionError, KeyError, TypeError):
            continue
        if best is None or nxt < best[0]:
            best = (nxt, cs, sc)
    if best is None:
        return None

    nxt, cs, sc = best
    return {
        "system_id": sc.id,
        "system": sc.name,
        "slug": sc.slug,
        # The signup form addresses systems by their legacy name, so the pitch
        # has to carry it or its "sign up for it" link lands nowhere. The
        # same-evening branch above gets this from club_nights_on already.
        "legacy_system_name": sc.legacy_system_name,
        "accent_color": cs.accent_color,
        "start_time": cs.session_start_time,
        "session_day": cs.session_day,
        "session_cadence": cs.session_cadence,
        "same_evening": False,
        "date": nxt.isoformat(),
    }
