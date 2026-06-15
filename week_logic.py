"""Week ID calculation and auto-pairings due-check logic.

week_id_for_system — faithful Python port of weekIdForSystem() in the
SvelteKit frontend (+page.server.ts). Must stay in sync with the frontend.

_is_auto_pairings_due — port of the original Streamlit pairings.py
due-check (lines 2871-2907): enabled gate, last-week dedup, day-of-week
match, and a 90-minute fire window starting at the configured time.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from run_hh_call_to_arms import hh_next_session_friday

_DAY_NAME_TO_INT: dict[str, int] = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
    "Friday": 4, "Saturday": 5, "Sunday": 6,
}


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _week_id_wed(today: date) -> str:
    """Next Wednesday — or this Wednesday if today is Mon/Tue/Wed.
    From Thursday onwards rolls to the following week's Wednesday."""
    w = today.weekday()  # Mon=0 … Sun=6
    if w >= 3:
        # Thu/Fri/Sat/Sun: jump to next Monday then add 2
        monday = today + timedelta(days=(7 - w))
        return _fmt(monday + timedelta(days=2))
    return _fmt(today + timedelta(days=(2 - w)))


def _week_id_fri(today: date) -> str:
    """Next Friday on or after today."""
    w = today.weekday()
    ahead = (4 - w) % 7  # 0 if today is Friday
    return _fmt(today + timedelta(days=ahead))


def week_id_for_system(system: str, d: date) -> str:
    """Return the canonical week-ID string (DD/MM/YYYY) for the upcoming
    session of the given system, relative to date d."""
    if system == "The Horus Heresy":
        return _fmt(hh_next_session_friday(d))
    if system == "Kill Team":
        return _week_id_fri(d)
    return _week_id_wed(d)


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
