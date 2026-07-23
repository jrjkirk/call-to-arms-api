"""Week ID calculation and auto-pairings due-check logic.

next_session_date / is_session_week — generic replacements for the old
per-system-name hardcoded date logic (_week_id_wed / _week_id_fri /
week_id_for_system), driven by ClubSystem.session_day/session_cadence/
cadence_anchor instead. This is now the single source of truth for
"what's the next session date for this club's system" — the frontend's
independent weekIdForSystem() duplicate is being retired in favour of
calling GET /week-id (main.py), which calls next_session_date() here.

_is_auto_pairings_due — port of the original Streamlit pairings.py
due-check (lines 2871-2907): enabled gate, last-week dedup, day-of-week
match, and a 90-minute fire window starting at the configured time.
"""
from datetime import date, datetime, timedelta
from typing import Optional

_DAY_NAME_TO_INT: dict[str, int] = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
    "Friday": 4, "Saturday": 5, "Sunday": 6,
}


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _next_weekly(day_name: str, today: date) -> date:
    """Next occurrence of day_name on or after today."""
    target = _DAY_NAME_TO_INT[day_name]
    ahead = (target - today.weekday()) % 7
    return today + timedelta(days=ahead)


def _next_fortnightly(day_name: str, cadence_anchor: date, today: date) -> date:
    """Next occurrence on the anchor's 14-day cycle, on or after today.
    Same algorithm as the old hh_next_session_friday (formerly in
    run_hh_call_to_arms.py, deleted), generalized off day_name/anchor
    instead of a hardcoded Friday + global constant."""
    if today <= cadence_anchor:
        return cadence_anchor
    delta_days = (today - cadence_anchor).days
    fortnights_passed = delta_days // 14
    candidate = cadence_anchor + timedelta(days=fortnights_passed * 14)
    if today > candidate:
        candidate += timedelta(days=14)
    return candidate


def next_session_date(
    session_day: str, session_cadence: str, cadence_anchor: Optional[date], today: date
) -> date:
    """The next session date for a club's system, given its ClubSystem
    schedule fields."""
    if session_cadence == "fortnightly":
        assert cadence_anchor is not None
        return _next_fortnightly(session_day, cadence_anchor, today)
    return _next_weekly(session_day, today)


def sessions_in_range(
    session_day: str, session_cadence: str, cadence_anchor: Optional[date],
    start: date, end: date,
) -> list[date]:
    """All session dates for a club's system falling within [start, end]
    (inclusive), for the Club-page calendar's auto-derived recurring
    sessions. Weekly: every occurrence of session_day in range. Fortnightly:
    every occurrence on the cadence_anchor's 14-day cycle in range."""
    if session_cadence == "fortnightly":
        assert cadence_anchor is not None
        dates: list[date] = []
        candidate = _next_fortnightly(session_day, cadence_anchor, start)
        while candidate <= end:
            if candidate >= start:
                dates.append(candidate)
            candidate += timedelta(days=14)
        return dates

    target = _DAY_NAME_TO_INT[session_day]
    dates = []
    candidate = start + timedelta(days=(target - start.weekday()) % 7)
    while candidate <= end:
        dates.append(candidate)
        candidate += timedelta(days=7)
    return dates


def is_session_week(
    session_cadence: str, cadence_anchor: Optional[date], next_session: date, today: date
) -> bool:
    """Generalizes the old is_hh_session_week — is a session happening
    within the next 7 days (i.e. is this an "on" week for a fortnightly
    club)? Always True for weekly."""
    if session_cadence == "weekly":
        return True
    days_until = (next_session - today).days
    return 0 <= days_until <= 6


def _is_auto_pairings_due(
    settings: dict,
    now_uk: datetime,
    target_week_id: str,
) -> bool:
    """Return True if auto-pairings should fire right now for this system.

    settings keys: enabled (bool), day (str), time ("HH:MM"), last_week (str|None).
    now_uk must be a timezone-aware datetime in Europe/London.
    """
    if not settings["enabled"]:
        return False
    last_week: Optional[str] = settings.get("last_week")
    if last_week and last_week == target_week_id:
        return False
    day_int = _DAY_NAME_TO_INT.get(settings["day"], 1)
    if now_uk.weekday() != day_int:
        return False
    h, m = map(int, settings["time"].split(":"))
    fire_start = now_uk.replace(hour=h, minute=m, second=0, microsecond=0)
    fire_end = fire_start + timedelta(minutes=90)
    return fire_start <= now_uk < fire_end


def _is_table_booking_cutoff_due(cutoff_day: str, cutoff_time: str, now_uk: datetime) -> bool:
    """Return True if a cutoff-mode table-booking send should fire right now.

    Unlike _is_auto_pairings_due, there's no last_week dedup parameter here —
    table_booking.py's send_table_booking_notification() already guards
    against a duplicate send for the same (club, system, week) by checking
    TableBookingNotification, so this only needs the day/time fire window.
    Same 90-minute window convention as the other due-checks, matching the
    hourly GitHub Actions cron cadence.

    now_uk must be a timezone-aware datetime in Europe/London.
    """
    day_int = _DAY_NAME_TO_INT.get(cutoff_day)
    if day_int is None or now_uk.weekday() != day_int:
        return False
    h, m = map(int, cutoff_time.split(":"))
    fire_start = now_uk.replace(hour=h, minute=m, second=0, microsecond=0)
    fire_end = fire_start + timedelta(minutes=90)
    return fire_start <= now_uk < fire_end


def _is_call_to_arms_due(
    settings: dict,
    now_uk: datetime,
    target_week_id: str,
    post_date: date,
) -> bool:
    """Return True if the call-to-arms post should fire right now.

    Same shape as _is_auto_pairings_due (enabled gate, last-week dedup, a
    90-minute fire window at the configured time), but scheduled relative to
    the club's session day: `post_date` is the session date minus
    `days_before`, so the caller decides *which* date to fire on and this
    just gates on today matching it plus the time window.

    settings keys: enabled (bool), time ("HH:MM"), last_week (str|None).
    now_uk must be a timezone-aware datetime in Europe/London.
    """
    if not settings["enabled"]:
        return False
    last_week: Optional[str] = settings.get("last_week")
    if last_week and last_week == target_week_id:
        return False
    if now_uk.date() != post_date:
        return False
    h, m = map(int, settings["time"].split(":"))
    fire_start = now_uk.replace(hour=h, minute=m, second=0, microsecond=0)
    fire_end = fire_start + timedelta(minutes=90)
    return fire_start <= now_uk < fire_end
