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
    Club, ClubSystem, Pairing, SystemConfig, User, VenueBooking, VenueClubNight,
    VenueConfig, VenueNightTable, VenueStaff, VenueTable,
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
    # No booking_hours: open hours live on Club.opening_hours now, edited in
    # the same place the club page reads them from.
    cfg = VenueConfig(club_id=club_id)
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


def opening_hours(db: Session, club_id: int) -> list[dict]:
    """The venue's open hours, always seven rows, each with an explicit
    `closed`.

    Stored on Club.opening_hours, which is ALSO what the public club page
    renders — one set of hours, edited once, in Venue Admin. There used to be a
    second copy on VenueConfig.booking_hours for "we're open before we take
    bookings"; that distinction cost venues two things to keep in step for a
    nuance none of them asked for, so it's gone and that column is now an
    orphan (same call as Club.leagues_enabled and ClubSystem.carousel_order).

    Club.opening_hours stores ONLY the open days — the club page treats a
    missing day as closed. This normalises to all seven so the editor and the
    availability engine never have to think about absence.
    """
    club = db.get(Club, club_id)
    by_day = {}
    for row in (club.opening_hours if club else None) or []:
        day_name = (row.get("day") or "").strip().title()
        if day_name in WEEKDAYS:
            by_day[day_name] = row
    out = []
    for name in WEEKDAYS:
        row = by_day.get(name)
        if row is None:
            out.append({"day": name, "open": None, "close": None,
                        "closed": True, "note": None})
        else:
            out.append({"day": name, "open": row.get("open"), "close": row.get("close"),
                        "closed": False, "note": row.get("note")})
    return out


def hours_for(db: Session, club_id: int, day: date) -> Optional[tuple[int, int]]:
    """(open, close) in minutes for a given date, or None if closed."""
    name = WEEKDAYS[day.weekday()]
    for row in opening_hours(db, club_id):
        if row["day"] == name:
            if row["closed"]:
                return None
            try:
                return to_minutes(row["open"]), to_minutes(row["close"])
            except (ValueError, TypeError):
                return None
    return None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Club nights and their tables
# ---------------------------------------------------------------------------

def club_nights(db: Session, club_id: int) -> list[VenueClubNight]:
    """Every night this venue hosts a plan for, in one list.

    Both kinds sit here: nights Call to Arms runs (system_id set) and nights it
    doesn't and may never — Magic, Bolt Action, Warmachine (system_id NULL).
    """
    return db.exec(
        select(VenueClubNight)
        .where(VenueClubNight.club_id == club_id)
        .where(VenueClubNight.active == True)
    ).all()


def night_for_system(db: Session, club_id: int, system_id: int) -> Optional[VenueClubNight]:
    return db.exec(
        select(VenueClubNight)
        .where(VenueClubNight.club_id == club_id)
        .where(VenueClubNight.system_id == system_id)
    ).first()


def get_or_create_night_for_system(
    db: Session, club_id: int, system_id: int
) -> VenueClubNight:
    """A system-backed night's plan row, made on demand.

    Needed because table assignments hang off the night, and a venue may want
    to hold tables for The Old World before it has any opinion about how many
    tables that night needs.
    """
    row = night_for_system(db, club_id, system_id)
    if row is not None:
        return row
    row = VenueClubNight(club_id=club_id, system_id=system_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def night_tables(db: Session, club_id: int) -> dict[int, dict[str, list[int]]]:
    """club_night_id -> {"preferred": [...], "reserved": [...]}.

    `preferred` includes held tables — reserving is a stronger statement of the
    same thing, and a caller asking "what suits this night" should never have to
    union two lists.
    """
    out: dict[int, dict[str, list[int]]] = {}
    for r in db.exec(
        select(VenueNightTable).where(VenueNightTable.club_id == club_id)
    ).all():
        entry = out.setdefault(r.club_night_id, {"preferred": [], "reserved": []})
        entry["preferred"].append(r.table_id)
        if r.reserved:
            entry["reserved"].append(r.table_id)
    return out


def tables_for_system(db: Session, club_id: int, system_id: Optional[int]) -> list[int]:
    """The tables that suit one game system, for the booking form's
    recommendations. Venue-only nights have no system, so they never match."""
    if system_id is None:
        return []
    night = night_for_system(db, club_id, system_id)
    if night is None:
        return []
    return night_tables(db, club_id).get(night.id, {}).get("preferred", [])


def reserved_table_ids_on(db: Session, club_id: int, day: date) -> set[int]:
    """Tables held back from the public on one date, across every club night
    running that day — whether or not this app runs the game.

    Keyed off which nights actually meet on that date, so a Wednesday
    reservation costs the venue nothing on a Tuesday.
    """
    by_night = night_tables(db, club_id)
    if not by_night:
        return set()
    running = {n["night_id"] for n in club_nights_on(db, club_id, day) if n["night_id"]}
    ids: set[int] = set()
    for night_id, entry in by_night.items():
        if night_id in running:
            ids.update(entry["reserved"])
    return ids


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
    system_id: Optional[int] = None,
    for_public: bool = True,
    reserved_ids: Optional[set[int]] = None,
    preferred_ids: Optional[set[int]] = None,
) -> list[VenueTable]:
    """Tables with nothing on them for [start, end), best first.

    Ordering carries real meaning: tables that suit the chosen game come first,
    then the smallest that still fits the party. So booking The Old World lands
    on a 6x4 rather than whatever happened to have the lowest id, and a pair
    doesn't eat the only big board.

    `for_public=False` is the staff path: it ignores club-night reservations,
    because someone standing in a half-empty room can see what the rule can't.

    `existing`, `tables`, `reserved_ids` and `preferred_ids` are accepted so the
    availability grid can load them once and reuse them across every slot it
    tests, instead of re-querying per slot.

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

    if for_public:
        held = reserved_table_ids_on(db, club_id, day) if reserved_ids is None else reserved_ids
        if held:
            out = [t for t in out if t.id not in held]

    if system_id is not None:
        if preferred_ids is None:
            preferred_ids = set(tables_for_system(db, club_id, system_id))
        out.sort(key=lambda t: (t.id not in preferred_ids, t.seats, t.sort_order, t.id))
    else:
        out.sort(key=lambda t: (t.seats, t.sort_order, t.id))
    return out


def availability(
    db: Session,
    club_id: int,
    day: date,
    duration_minutes: Optional[int] = None,
    party_size: Optional[int] = None,
    system_id: Optional[int] = None,
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
    window = hours_for(db, club_id, day)
    tables = active_tables(db, club_id)

    base = {
        "date": day.isoformat(),
        "enabled": True,
        "duration_minutes": duration,
        "tables": len(tables),
        "slot_minutes": cfg.slot_minutes,
        # Named so the booking page can explain a thin evening honestly: "six of
        # our tables are held for the Old World night" beats an unexplained grid
        # of greyed-out slots.
        "club_nights": [
            {"system": n["system"], "system_id": n["system_id"],
             "start_time": n["start_time"],
             # The signup form addresses systems by their legacy name, which is
             # NOT SystemConfig.name (see the note on that field) — a link built
             # from the display name only works by coincidence.
             "legacy_system_name": n["legacy_system_name"]}
            for n in club_nights_on(db, club_id, day)
        ],
    }
    base["tables_held"] = len(reserved_table_ids_on(db, club_id, day))
    if window is None:
        return {**base, "slots": [], "closed": True}
    if not tables:
        return {**base, "slots": [], "no_tables": True}

    open_m, close_m = window
    now = club_now(db, club_id)
    today = now.date()
    cutoff = now.hour * 60 + now.minute + cfg.lead_time_minutes

    existing = bookings_on(db, club_id, day)
    # Loaded once for the whole grid rather than per slot — reserved_table_ids_on
    # derives the club nights running that day, which is not a query to repeat
    # twenty times for one evening.
    reserved_ids = reserved_table_ids_on(db, club_id, day)
    preferred_ids = (
        set(tables_for_system(db, club_id, system_id))
        if system_id is not None else set()
    )

    slots = []
    start = open_m
    while start + duration <= close_m:
        end = start + duration
        reason = None
        free = free_tables_for(
            db, club_id, day, start, end,
            existing=existing, tables=tables, party_size=party_size,
            system_id=system_id, reserved_ids=reserved_ids, preferred_ids=preferred_ids,
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
            # So the form can say "2 tables that suit The Old World" rather than
            # a bare count that might all be the wrong size.
            "preferred_free": sum(1 for t in free if t.id in preferred_ids),
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

class DiaryContext:
    """Everything the diary needs, loaded ONCE for a whole date range.

    A month view is thirty-one day_overview calls, and each of those wants the
    club-night definitions, that week's signups, the day's bookings and the
    table list. Done naively that is several hundred queries for one screen —
    exactly the shape that exhausted the connection pool when /pairings was
    scanning per request. So the range is fetched flat and the per-day work
    becomes dictionary lookups.
    """

    def __init__(self, db: Session, club_id: int, first: date, last: date):
        from week_logic import _fmt
        from models import Signup

        self.club_id = club_id
        self.tables = active_tables(db, club_id)
        self.plans = club_nights(db, club_id)
        self.by_system = {p.system_id: p for p in self.plans if p.system_id is not None}
        self.reserved = night_tables(db, club_id)
        self.systems = db.exec(
            select(ClubSystem, SystemConfig)
            .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
            .where(ClubSystem.club_id == club_id)
            .where(ClubSystem.enabled == True)
            .where(SystemConfig.active == True)
        ).all()

        rows = db.exec(
            select(VenueBooking)
            .where(VenueBooking.club_id == club_id)
            .where(VenueBooking.booking_date >= first)
            .where(VenueBooking.booking_date <= last)
            .where(VenueBooking.status.in_(BLOCKING_STATUSES))
        ).all()
        self.bookings: dict[date, list[VenueBooking]] = {}
        for b in rows:
            self.bookings.setdefault(b.booking_date, []).append(b)

        # One query for every signup in the window, bucketed by (week, system).
        weeks = set()
        d = first
        while d <= last:
            weeks.add(_fmt(d))
            d += timedelta(days=1)
        sign_rows = db.exec(
            select(Signup)
            .where(Signup.club_id == club_id)
            .where(Signup.week.in_(sorted(weeks)))
        ).all()
        self.signups: dict[tuple[str, str], int] = {}
        for su in sign_rows:
            key = (su.week, su.system)
            self.signups[key] = self.signups.get(key, 0) + 1


def club_nights_on(db: Session, club_id: int, day: date) -> list[dict]:
    """Every club night running at this venue on one date.

    Two sources, deliberately merged here so nothing downstream has to know the
    difference:

      Call to Arms nights come from each ClubSystem's own schedule, exactly as
        the club calendar derives them, so a venue never re-enters its own game
        nights and the two can't drift apart. Signups are counted.
      Venue-only nights (Magic, Bolt Action, Warmachine) come from
        VenueClubNight, where staff entered the day and cadence themselves.
        Nobody signs up to these, so `signups` is 0 and how much room they need
        can only come from the plan or the tables held for them.

    Every entry carries `night_id` (the VenueClubNight row, when there is one)
    and `system_id` (None for venue-only). Reservations key on night_id;
    anything about signups, pairings or the cross-sell keys on system_id and so
    naturally skips a night this app knows nothing about.
    """
    from week_logic import sessions_in_range, _fmt  # local: avoids an import cycle
    from models import Signup

    plans = club_nights(db, club_id)
    by_system = {p.system_id: p for p in plans if p.system_id is not None}
    reserved = night_tables(db, club_id)

    def _runs_on(day_name: Optional[str], cadence: Optional[str],
                 anchor: Optional[date]) -> bool:
        if not day_name:
            return False
        try:
            return bool(sessions_in_range(day_name, cadence or "weekly", anchor, day, day))
        except (AssertionError, KeyError):
            # Fortnightly with no anchor, or an unrecognised day name. One
            # misconfigured night must not blank the venue's whole diary.
            return False

    out = []

    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
    ).all()
    for cs, sc in rows:
        if not _runs_on(cs.session_day, cs.session_cadence, cs.cadence_anchor):
            continue
        signups = db.exec(
            select(Signup)
            .where(Signup.club_id == club_id)
            .where(Signup.system == sc.legacy_system_name)
            .where(Signup.week == _fmt(day))
        ).all()

        # Three ways to say how much room this night needs, best first:
        #   the venue's own plan (a human decided it)
        #   the tables actually held for it
        #   two players to a table from signups (the fallback estimate, the
        #     same assumption TableBookingConfig defaults to)
        plan = by_system.get(sc.id)
        held = len(reserved.get(plan.id, {}).get("reserved", [])) if plan else 0
        estimate = -(-len(signups) // 2)
        planned = plan.expected_tables if plan and plan.expected_tables else None
        out.append({
            "night_id": plan.id if plan else None,
            "system_id": sc.id,
            "system": sc.name,
            "slug": sc.slug,
            "legacy_system_name": sc.legacy_system_name,
            "accent_color": cs.accent_color,
            "start_time": cs.session_start_time,
            "app_managed": True,
            "signups": len(signups),
            "tables_planned": planned,
            "tables_held": held,
            "tables_estimated": estimate,
            "tables_expected": planned or held or estimate,
            # So the diary can flag a night whose signups have outgrown the room
            # set aside for it, while there's still time to lay out more.
            "outgrown": bool((planned or held) and estimate > (planned or held)),
        })

    for p in plans:
        if p.system_id is not None:
            continue
        if not _runs_on(p.session_day, p.session_cadence, p.cadence_anchor):
            continue
        held = len(reserved.get(p.id, {}).get("reserved", []))
        planned = p.expected_tables or None
        out.append({
            "night_id": p.id,
            "system_id": None,
            "system": p.name or "Club night",
            "slug": None,
            "legacy_system_name": None,
            "accent_color": None,
            "start_time": p.start_time,
            "app_managed": False,
            # Nobody signs up to a night this app doesn't run, so there is no
            # estimate to fall back on — 0 here is a fact, not a missing value,
            # and `outgrown` can never fire for one.
            "signups": 0,
            "tables_planned": planned,
            "tables_held": held,
            "tables_estimated": 0,
            "tables_expected": planned or held or 0,
            "outgrown": False,
        })

    return sorted(out, key=lambda r: (r["start_time"] or "", r["system"]))


def table_review(db: Session, club_id: int, night: VenueClubNight,
                 sessions: int = 8) -> dict:
    """Hold a club night's table forecast against what actually happened.

    A forecast nobody checks is a guess with better posture, so this compares
    expected_tables with the real thing: PUBLISHED PAIRINGS, one pairing to a
    table, over recent sessions.

    Pairings rather than signups deliberately. Signups are an intention — they
    include people who don't turn up and people who drop after the deadline —
    while a pairing is two players the club actually sat down opposite each
    other. A BYE has no opponent and takes no table, so it isn't counted.

    A VENUE-ONLY NIGHT HAS NONE OF THIS. Magic and Bolt Action don't run
    through this app, so there is nothing to measure and no honest average to
    report. It says so plainly instead of returning zeros, which would read as
    "your night is empty" and advise the venue to take tables away from it.
    """
    if night.system_id is None:
        return {
            "night_id": night.id,
            "system_id": None,
            "system": night.name or "Club night",
            "session_day": night.session_day,
            "expected_tables": night.expected_tables,
            "samples": [],
            "average_tables": None,
            "busiest_tables": None,
            "measurable": False,
            "advice": None,
        }

    from week_logic import sessions_in_range, _fmt

    row = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.system_id == night.system_id)
    ).first()
    if row is None:
        return {"night_id": night.id, "system_id": night.system_id,
                "samples": [], "measurable": False, "advice": None}
    cs, sc = row

    expected = night.expected_tables
    today = club_now(db, club_id).date()
    # Fortnightly needs twice the calendar reach for the same sample count.
    span = sessions * (14 if cs.session_cadence == "fortnightly" else 7)
    try:
        dates = sessions_in_range(
            cs.session_day, cs.session_cadence, cs.cadence_anchor,
            today - timedelta(days=span), today,
        )
    except (AssertionError, KeyError):
        dates = []

    samples = []
    for d in sorted(dates, reverse=True)[:sessions]:
        week = _fmt(d)
        pairings = db.exec(
            select(Pairing)
            .where(Pairing.club_id == club_id)
            .where(Pairing.system == sc.legacy_system_name)
            .where(Pairing.week == week)
        ).all()
        # A week with no pairings was a week nobody ran the numbers, not a week
        # nobody came. Averaging that in as a zero would quietly advise the
        # venue to lay out fewer tables.
        if not pairings:
            continue
        played = [p for p in pairings if p.b_signup_id is not None]
        samples.append({"date": d.isoformat(), "week": week, "tables_used": len(played)})

    samples.reverse()
    used = [s["tables_used"] for s in samples]
    average = round(sum(used) / len(used), 1) if used else None
    busiest = max(used) if used else None

    return {
        "night_id": night.id,
        "system_id": sc.id,
        "system": sc.name,
        "session_day": cs.session_day,
        "expected_tables": expected,
        "samples": samples,
        "average_tables": average,
        "busiest_tables": busiest,
        "measurable": True,
        "advice": _table_advice(expected, average, busiest),
    }


def _table_advice(
    expected: Optional[int], average: Optional[float], busiest: Optional[int]
) -> Optional[str]:
    """One sentence a venue can act on, or nothing.

    Advises against the BUSIEST recent session rather than the average: laying
    out to the mean means turning people away half the time, and an unused
    table costs a venue far less than a player with nowhere to play.
    """
    if average is None:
        return None
    if expected is None:
        return (f"No plan set. Recent sessions used {average} tables on average, "
                f"{busiest} at the busiest.")
    if busiest > expected:
        return (f"Set aside {expected}, but the busiest recent session needed "
                f"{busiest}. Consider {busiest}.")
    if busiest <= expected - 2:
        return (f"Set aside {expected}, but the busiest recent session only used "
                f"{busiest}. You could free up {expected - busiest}.")
    return f"{expected} looks right — recent sessions peaked at {busiest}."


def range_overview(db: Session, club_id: int, first: date, last: date) -> list[dict]:
    """day_overview for every date in a range, without the query storm.

    A month view is thirty-one days, and day_overview asks for the club-night
    definitions, that week's signups, the day's bookings and the table list
    each time. Looped naively that is several hundred queries for one screen —
    the same shape that exhausted the connection pool when /pairings was
    scanning per request. So the whole window is fetched flat here and the
    per-day work becomes dictionary lookups.
    """
    from week_logic import _fmt
    from models import Signup

    tables = active_tables(db, club_id)
    total = len(tables)
    plans = club_nights(db, club_id)
    by_system = {p.system_id: p for p in plans if p.system_id is not None}
    reserved = night_tables(db, club_id)
    systems = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
    ).all()

    booking_rows = db.exec(
        select(VenueBooking)
        .where(VenueBooking.club_id == club_id)
        .where(VenueBooking.booking_date >= first)
        .where(VenueBooking.booking_date <= last)
        .where(VenueBooking.status.in_(BLOCKING_STATUSES))
    ).all()
    bookings_by_day: dict[date, list[VenueBooking]] = {}
    for b in booking_rows:
        bookings_by_day.setdefault(b.booking_date, []).append(b)

    weeks = set()
    d = first
    while d <= last:
        weeks.add(_fmt(d))
        d += timedelta(days=1)
    counts: dict[tuple[str, str], int] = {}
    for su in db.exec(
        select(Signup)
        .where(Signup.club_id == club_id)
        .where(Signup.week.in_(sorted(weeks)))
    ).all():
        key = (su.week, su.system)
        counts[key] = counts.get(key, 0) + 1

    from week_logic import sessions_in_range

    def runs_on(day: date, day_name, cadence, anchor) -> bool:
        if not day_name:
            return False
        try:
            return bool(sessions_in_range(day_name, cadence or "weekly", anchor, day, day))
        except (AssertionError, KeyError):
            return False

    out = []
    day = first
    while day <= last:
        nights = []
        for cs, sc in systems:
            if not runs_on(day, cs.session_day, cs.session_cadence, cs.cadence_anchor):
                continue
            plan = by_system.get(sc.id)
            held = len(reserved.get(plan.id, {}).get("reserved", [])) if plan else 0
            signups = counts.get((_fmt(day), sc.legacy_system_name), 0)
            estimate = -(-signups // 2)
            planned = plan.expected_tables if plan and plan.expected_tables else None
            nights.append({
                "night_id": plan.id if plan else None, "system_id": sc.id,
                "system": sc.name, "accent_color": cs.accent_color,
                "start_time": cs.session_start_time, "app_managed": True,
                "signups": signups, "tables_planned": planned, "tables_held": held,
                "tables_estimated": estimate,
                "tables_expected": planned or held or estimate,
                "outgrown": bool((planned or held) and estimate > (planned or held)),
            })
        for p in plans:
            if p.system_id is not None:
                continue
            if not runs_on(day, p.session_day, p.session_cadence, p.cadence_anchor):
                continue
            held = len(reserved.get(p.id, {}).get("reserved", []))
            planned = p.expected_tables or None
            nights.append({
                "night_id": p.id, "system_id": None, "system": p.name or "Club night",
                "accent_color": None, "start_time": p.start_time, "app_managed": False,
                "signups": 0, "tables_planned": planned, "tables_held": held,
                "tables_estimated": 0, "tables_expected": planned or held or 0,
                "outgrown": False,
            })
        nights.sort(key=lambda r: (r["start_time"] or "", r["system"]))

        day_bookings = bookings_by_day.get(day, [])
        booked_ids = {b.table_id for b in day_bookings}
        held_ids: set[int] = set()
        for n in nights:
            if n["night_id"]:
                held_ids.update(reserved.get(n["night_id"], {}).get("reserved", []))
        night_demand = sum(n["tables_expected"] for n in nights)
        committed = len(booked_ids) + night_demand - len(booked_ids & held_ids)

        out.append({
            "date": day.isoformat(),
            "weekday": WEEKDAYS[day.weekday()],
            "tables_total": total,
            "tables_booked": len(booked_ids),
            "tables_club_night": night_demand,
            "tables_held": len(held_ids),
            "tables_committed": committed,
            "load": round(committed / total, 3) if total else None,
            "over_capacity": total > 0 and committed > total,
            "outgrown": any(n["outgrown"] for n in nights),
            "club_nights": nights,
            "bookings": len(day_bookings),
            "events": len({b.event_id for b in day_bookings if b.event_id}),
        })
        day += timedelta(days=1)
    return out


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
    night_demand = sum(n["tables_expected"] for n in nights)
    total = len(tables)

    # A held table that has also been booked would otherwise be counted twice —
    # staff CAN seat someone on a reserved table, and when they do it is one
    # table doing one job, not two tables' worth of demand.
    held_ids = reserved_table_ids_on(db, club_id, day)
    double_counted = len({b.table_id for b in bookings} & held_ids)
    committed = booked_tables + night_demand - double_counted

    return {
        "date": day.isoformat(),
        "weekday": WEEKDAYS[day.weekday()],
        "enabled": bool(cfg and cfg.enabled),
        "tables_total": total,
        "tables_booked": booked_tables,
        "tables_club_night": night_demand,
        "tables_held": len(held_ids),
        "tables_committed": committed,
        # A night whose signups have outgrown the room set aside for it — the
        # warning worth acting on while there's still time to lay out more.
        "outgrown": any(n["outgrown"] for n in nights),
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
    """The cross-sell: the club night for the game they've just booked to play.

    ONLY ever pitches the system they booked for. Someone who books a table for
    The Old World is exactly the person who wants the Old World night; telling
    them about 40k instead is a leaflet through the door. Matching the system
    turns the pitch into something they were already looking for.

    Two shapes, both good invitations for different reasons:

      same evening — the night is running on the very date they've booked.
        They're coming anyway, and a room of players is already signed up, so
        the ask is "sign up and get paired" rather than "come back another time".
      otherwise — the next date that system runs here, so a Tuesday booking for
        The Old World learns there's an Old World night every Wednesday.

    Returns None when they picked "Something else", or left the game blank:
    there's no system to match, and anything we offered would be a guess.
    """
    cfg = get_config(db, club_id)
    if cfg is None or not cfg.promote_club_nights:
        return None
    if booking.system_id is None:
        return None

    tonight = [
        n for n in club_nights_on(db, club_id, booking.booking_date)
        if n["system_id"] == booking.system_id
    ]
    if tonight:
        return {**tonight[0], "same_evening": True,
                "date": booking.booking_date.isoformat()}

    from week_logic import next_session_date

    row = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
        .where(ClubSystem.system_id == booking.system_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
    ).first()
    if row is None:
        return None
    cs, sc = row

    try:
        nxt = next_session_date(
            cs.session_day, cs.session_cadence, cs.cadence_anchor,
            booking.booking_date,
        )
    except (AssertionError, KeyError, TypeError):
        # Fortnightly with no anchor, or an unrecognised day name. No date we
        # can stand behind, so say nothing rather than guess one.
        return None

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


# ---------------------------------------------------------------------------
# The floor plan
#
# Everything here works in FEET (see the note on VenueTable). Positions are the
# CENTRE of a thing, so rotation is one transform about that point.
# ---------------------------------------------------------------------------

# The palette a venue may colour-code its plan with. Named rather than free
# hex: the plan is dark, and "green" is already how the Tonight view says
# "free", so an unconstrained picker would let someone build a room that
# contradicts the one view staff read fastest.
TABLE_COLORS = ("slate", "blue", "green", "amber", "red", "purple", "teal", "grey")

# What a fixture may be. Validated on save so a typo can't put something on the
# plan that nothing knows how to draw.
FEATURE_KINDS = (
    "enclosure", "wall", "note", "bar", "door", "pillar", "shelves",
    "stairs", "toilets",
)

# Structure, not furniture: these ignore the colour palette. A plan where the
# walls are teal stops reading as a building.
STRUCTURAL_KINDS = ("enclosure", "wall", "door")

# One wall thickness for the whole plan — the room's own boundary, an
# enclosure's walls, and a standalone wall segment. They are the same material,
# so anything else makes them fail to line up where they meet.
WALL_FT = 0.5

STANDARD_TABLE = (6.0, 4.0)
DEFAULT_ROOM = (30.0, 20.0)


def rooms_for(db: Session, club_id: int) -> list:
    from models import VenueRoom
    return db.exec(
        select(VenueRoom)
        .where(VenueRoom.club_id == club_id)
        .order_by(VenueRoom.sort_order, VenueRoom.id)
    ).all()


def ensure_default_room(db: Session, club_id: int):
    """Give a venue somewhere to put things the first time they open the plan,
    and lay their existing tables out in it.

    A venue that already has eight tables shouldn't be met with an empty canvas
    and a filing job — the tables exist, they're just not placed yet. Arranging
    them in tidy rows turns the first visit into "drag these into the right
    places" rather than "build your venue from nothing".
    """
    from models import VenueRoom, VenueTable

    existing = rooms_for(db, club_id)
    if existing:
        return existing[0]

    room = VenueRoom(club_id=club_id, name="Main room",
                     width_ft=DEFAULT_ROOM[0], depth_ft=DEFAULT_ROOM[1])
    db.add(room)
    db.commit()
    db.refresh(room)
    autoplace(db, club_id, room)
    return room


def autoplace(db: Session, club_id: int, room) -> int:
    """Lay every unplaced table out in rows inside a room, and widen the room if
    they don't fit. Returns how many were placed.

    Deliberately grows the room rather than overlapping or dropping tables: a
    venue's real room is whatever size it is, and a plan that silently loses
    table 9 is worse than one that starts too big and gets resized.
    """
    from models import VenueTable

    unplaced = db.exec(
        select(VenueTable)
        .where(VenueTable.club_id == club_id)
        .where(VenueTable.room_id.is_(None))
        .order_by(VenueTable.sort_order, VenueTable.id)
    ).all()
    if not unplaced:
        return 0

    gap = 3.0                      # walking room between tables
    margin = 2.0                   # from the walls
    x = margin
    y = margin
    row_depth = 0.0
    placed = 0

    for t in unplaced:
        w = t.width_ft or STANDARD_TABLE[0]
        d = t.depth_ft or STANDARD_TABLE[1]
        if x + w + margin > room.width_ft and x > margin:
            x = margin
            y += row_depth + gap
            row_depth = 0.0
        t.room_id = room.id
        t.pos_x = round(x + w / 2, 1)
        t.pos_y = round(y + d / 2, 1)
        t.width_ft, t.depth_ft = w, d
        t.rotation = t.rotation or 0.0
        db.add(t)
        x += w + gap
        row_depth = max(row_depth, d)
        placed += 1

    needed = y + row_depth + margin
    if needed > room.depth_ft:
        room.depth_ft = round(needed, 1)
        db.add(room)

    db.commit()
    return placed


def size_label_for(width_ft: float, depth_ft: float) -> str:
    """"6x4" from the dimensions, so the label can't disagree with the shape.

    It used to be typed by hand next to a seat count, which meant a table could
    read "6x4" on the booking page while being drawn 4ft square on the plan.
    """
    def fmt(v: float) -> str:
        return str(int(v)) if float(v).is_integer() else f"{v:g}"
    return f"{fmt(width_ft)}x{fmt(depth_ft)}"


def layout(db: Session, club_id: int) -> dict:
    """The whole plan: rooms, their tables, and their fixtures."""
    from models import VenueFeature, VenueTable

    rooms = rooms_for(db, club_id)
    tables = db.exec(
        select(VenueTable)
        .where(VenueTable.club_id == club_id)
        .order_by(VenueTable.sort_order, VenueTable.id)
    ).all()
    features = db.exec(
        select(VenueFeature).where(VenueFeature.club_id == club_id)
    ).all()

    return {
        "rooms": [
            {"id": r.id, "name": r.name, "width_ft": r.width_ft,
             "depth_ft": r.depth_ft, "sort_order": r.sort_order, "notes": r.notes}
            for r in rooms
        ],
        "tables": [
            {"id": t.id, "name": t.name, "room_id": t.room_id, "shape": t.shape, "color": t.color,
             "pos_x": t.pos_x, "pos_y": t.pos_y,
             "width_ft": t.width_ft, "depth_ft": t.depth_ft,
             "rotation": t.rotation, "seats": t.seats, "active": t.active,
             "size_label": t.size_label, "notes": t.notes,
             "sort_order": t.sort_order}
            for t in tables
        ],
        "features": [
            {"id": f.id, "room_id": f.room_id, "kind": f.kind, "label": f.label,
             "shape": f.shape, "color": f.color,
             "pos_x": f.pos_x, "pos_y": f.pos_y, "width_ft": f.width_ft,
             "depth_ft": f.depth_ft, "rotation": f.rotation,
             "flip_h": f.flip_h, "flip_v": f.flip_v}
            for f in features
        ],
    }


def occupancy(db: Session, club_id: int, day: date, at: Optional[str] = None) -> dict:
    """What each table is doing on a date — the plan as a view of tonight.

    This is the reason the floor plan is worth building rather than a list.
    Staff standing at the door with "is anything free at eight" get an answer
    shaped like the room they're looking at, not a table of times.

    `at` narrows it to one moment ("what's on right now"); without it the whole
    day's bookings come back per table.
    """
    from models import VenueEvent

    rows = bookings_on(db, club_id, day)
    moment = None
    if at:
        try:
            moment = to_minutes(at)
        except ValueError:
            moment = None

    events = {
        e.id: e for e in db.exec(
            select(VenueEvent).where(VenueEvent.club_id == club_id)
            .where(VenueEvent.event_date == day)
        ).all()
    }

    per_table: dict[int, list[dict]] = {}
    for b in rows:
        try:
            s, e = to_minutes(b.start_time), to_minutes(b.end_time)
        except ValueError:
            continue
        if moment is not None and not (s <= moment < e):
            continue
        ev = events.get(b.event_id) if b.event_id else None
        per_table.setdefault(b.table_id, []).append({
            "booking_id": b.id,
            "start": b.start_time,
            "end": b.end_time,
            "name": ev.name if ev else b.contact_name,
            "party_size": b.party_size,
            "status": b.status,
            "is_event": ev is not None,
            "event_status": ev.status if ev else None,
        })

    held = reserved_table_ids_on(db, club_id, day)
    nights = club_nights_on(db, club_id, day)
    return {
        "date": day.isoformat(),
        "at": at,
        "tables": {str(k): v for k, v in per_table.items()},
        "held_table_ids": sorted(held),
        "club_nights": [{"system": n["system"], "start_time": n["start_time"],
                         "accent_color": n["accent_color"]} for n in nights],
    }
