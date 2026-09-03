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


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> Optional[date]:
    """The nth occurrence of a weekday in a month, or None if there isn't one.

    nth is 1-based. A month with only four Wednesdays has no fifth, and
    returning None rather than spilling into the next month is what keeps
    "first Wednesday" meaning the first Wednesday.
    """
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (nth - 1) * 7
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_monthly(day_name: str, cadence_anchor: date, today: date) -> date:
    """Next occurrence of "the nth <weekday> of the month", on or after today.

    Which nth comes from the ANCHOR: a club that told us it last met on the
    second Wednesday meets on the second Wednesday. Asking separately for an
    ordinal would be a second question about a fact they've already given us.

    A month without that many of the weekday is skipped rather than
    approximated — a "fifth Friday" club genuinely doesn't meet that month.
    """
    weekday = _DAY_NAME_TO_INT[day_name]
    nth = (cadence_anchor.day - 1) // 7 + 1
    year, month = today.year, today.month
    for _ in range(14):                      # a year and a bit of headroom
        candidate = _nth_weekday_of_month(year, month, weekday, nth)
        if candidate is not None and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return _next_weekly(day_name, today)     # unreachable in practice


def next_session_date(
    session_day: str, session_cadence: str, cadence_anchor: Optional[date], today: date
) -> date:
    """The next session date for a club's system, given its ClubSystem
    schedule fields."""
    if session_cadence == "fortnightly":
        assert cadence_anchor is not None
        return _next_fortnightly(session_day, cadence_anchor, today)
    if session_cadence == "monthly":
        assert cadence_anchor is not None
        return _next_monthly(session_day, cadence_anchor, today)
    return _next_weekly(session_day, today)


def sessions_in_range(
    session_day: str, session_cadence: str, cadence_anchor: Optional[date],
    start: date, end: date,
) -> list[date]:
    """All session dates for a club's system falling within [start, end]
    (inclusive), for the Club-page calendar's auto-derived recurring
    sessions. Weekly: every occurrence of session_day in range. Fortnightly:
    every occurrence on the cadence_anchor's 14-day cycle in range. Monthly:
    the nth weekday of each month, where n comes from the anchor."""
    if session_cadence == "monthly":
        assert cadence_anchor is not None
        dates = []
        candidate = _next_monthly(session_day, cadence_anchor, start)
        while candidate <= end:
            dates.append(candidate)
            # Step into next month and re-derive, rather than adding 28 days:
            # four weeks drifts off "the second Wednesday" within a year.
            nxt = candidate.replace(day=1) + timedelta(days=32)
            candidate = _next_monthly(session_day, cadence_anchor, nxt.replace(day=1))
        return dates

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
    # Open-ended to the end of the configured day, for the reason set out in
    # _is_call_to_arms_due: the "hourly" cron is nothing of the sort, and a
    # 90-minute window silently cost a club a week's post. Repeats are
    # harmless because last_week above already deduped this target week.
    return now_uk >= fire_start


def _is_league_rankings_due(settings: dict, now_uk: datetime, today_key: str) -> bool:
    """Return True if this club-system's league standings post is due now.

    settings keys: day (str), time ("HH:MM"), last_posted (ISO date str|None).
    `today_key` is now_uk.date().isoformat() — the dedup is per calendar day
    rather than per week id, because this post isn't tied to a session date.

    There is deliberately no `enabled` key. Whether a club posts its standings
    at all is already the `league` posting switch, which also governs results
    and achievements; a second switch for the same decision is a second thing
    to leave in the wrong position. This answers only "is it time?".

    Same shape as the other due-checks: fires from the configured time to the
    end of the configured day and never onto the next one. That window matters
    more here than anywhere else — this used to be a `0 19 * * 4` GitHub cron,
    which has exactly one slot a week, and the observed run times never once
    landed on it: 21:00, 21:44, 22:22, and twice past midnight into Friday
    (01:39 and 03:24), where nobody saw the post at all.
    """
    last_posted: Optional[str] = settings.get("last_posted")
    if last_posted and last_posted == today_key:
        return False
    day_int = _DAY_NAME_TO_INT.get(settings["day"])
    if day_int is None or now_uk.weekday() != day_int:
        return False
    h, m = map(int, settings["time"].split(":"))
    fire_start = now_uk.replace(hour=h, minute=m, second=0, microsecond=0)
    return now_uk >= fire_start


def _is_table_booking_cutoff_due(cutoff_day: str, cutoff_time: str, now_uk: datetime) -> bool:
    """Return True if a cutoff-mode table-booking send should fire right now.

    Unlike _is_auto_pairings_due, there's no last_week dedup parameter here —
    table_booking.py's send_table_booking_notification() already guards
    against a duplicate send for the same (club, system, week) by checking
    TableBookingNotification, so this only needs the day/time fire window.
    Fires from the cutoff time to the end of the cutoff day, for the reason
    set out in _is_call_to_arms_due — the cron this rides on is far less
    punctual than "hourly" suggests. Safe to re-evaluate on every tick because
    of the TableBookingNotification guard described above.

    now_uk must be a timezone-aware datetime in Europe/London.
    """
    day_int = _DAY_NAME_TO_INT.get(cutoff_day)
    if day_int is None or now_uk.weekday() != day_int:
        return False
    h, m = map(int, cutoff_time.split(":"))
    fire_start = now_uk.replace(hour=h, minute=m, second=0, microsecond=0)
    return now_uk >= fire_start


def _is_call_to_arms_due(
    settings: dict,
    now_uk: datetime,
    target_week_id: str,
    post_date: date,
) -> bool:
    """Return True if the call-to-arms post should fire right now.

    Fires on `post_date` (the session date minus `days_before`) at or after the
    club's configured time, once per target week — the last_week dedup is what
    makes "at or after" safe to repeat on every hourly tick.

    This used to close 90 minutes after the configured time, which gave an
    hourly cron exactly two chances to land. GitHub Actions does not promise
    punctual scheduled runs — it delays them under load and drops them
    outright — so two missed ticks silently cost a club its whole week's post,
    with no catch-up and nothing in the logs to say a post had been due. That
    is what happened to The Old World at EGNWGC for the 02/09/2026 session.

    It still never posts on a day other than the one configured: a run that is
    late by hours now catches up, a run that is late by a day does not, because
    a "sign up for Wednesday" message arriving on Tuesday night is worse than
    one that never arrives.

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
    return now_uk >= fire_start
