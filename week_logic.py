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
    Same algorithm as the old hh_next_session_friday (run_hh_call_to_arms.py,
    still used independently by that script's own weekly-reminder post —
    not touched here), generalized off day_name/anchor instead of a
    hardcoded Friday + global constant."""
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
