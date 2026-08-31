"""The running order for an event: rounds, and everything between them.

A schedule is a list of items, each {kind, day, start, end, round?, label?}:

    {"kind": "round", "day": 1, "round": 1, "start": "09:30", "end": "12:00"}
    {"kind": "break", "day": 1, "label": "Lunch", "start": "12:00", "end": "13:00"}

Generated to a sensible default from rounds/days/round_minutes so a first-time
TO never faces an empty grid, then edited freely — the generated version is a
starting point, not a constraint.
"""
from datetime import date, datetime, timedelta
from typing import Optional

DEFAULT_START = "09:30"
DEFAULT_GAP_MINUTES = 30        # between rounds: scores in, next pairing up
DEFAULT_LUNCH_MINUTES = 45
# The earliest a generated lunch break is placed.
LUNCH_FROM = "12:00"


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hhmm(mins: int) -> str:
    mins %= 24 * 60
    return f"{mins // 60:02d}:{mins % 60:02d}"


def generate(tournament) -> list[dict]:
    """A default running order. Rounds spread as evenly as possible across the
    days, with a lunch break dropped in the middle of any day carrying three or
    more rounds — which is where a day without one starts to hurt."""
    days = max(1, tournament.days or 1)
    total = max(1, tournament.rounds or 1)
    length = max(30, tournament.round_minutes or 150)
    start_at = tournament.start_time or DEFAULT_START

    # Spread rounds across days: earlier days take the extra when it doesn't
    # divide evenly, because people leave early on the last day, not the first.
    per_day = [total // days] * days
    for i in range(total % days):
        per_day[i] += 1

    out: list[dict] = []
    round_no = 1
    for day in range(1, days + 1):
        clock = _mins(start_at)
        count = per_day[day - 1]
        lunched = count < 2      # a single-round day needs no lunch
        for i in range(1, count + 1):
            out.append({
                "kind": "round", "day": day, "round": round_no,
                "start": _hhmm(clock), "end": _hhmm(clock + length),
            })
            clock += length
            round_no += 1

            if i == count:
                break
            # Lunch goes after the first round that finishes at or after
            # LUNCH_FROM, not after the arithmetically middle round. Five long
            # rounds in a day put "the middle" at six in the evening, which is
            # not lunch by any reading.
            if not lunched and clock >= _mins(LUNCH_FROM):
                out.append({
                    "kind": "break", "day": day, "label": "Lunch",
                    "start": _hhmm(clock), "end": _hhmm(clock + DEFAULT_LUNCH_MINUTES),
                })
                clock += DEFAULT_LUNCH_MINUTES
                lunched = True
            else:
                clock += DEFAULT_GAP_MINUTES
    return out


def normalise(items: list, tournament) -> list[dict]:
    """Clean a TO-supplied schedule: keep only known keys, sort by day then
    time, and renumber rounds in the order they actually run — a TO who drags
    round three above round two means the running order, not the label."""
    cleaned = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind") if raw.get("kind") in ("round", "break") else "break"
        try:
            start = _hhmm(_mins(str(raw.get("start") or DEFAULT_START)))
            end = _hhmm(_mins(str(raw.get("end") or raw.get("start") or DEFAULT_START)))
        except (ValueError, AttributeError):
            continue
        cleaned.append({
            "kind": kind,
            "day": max(1, int(raw.get("day") or 1)),
            "start": start, "end": end,
            **({"label": str(raw.get("label") or "Break")[:60]} if kind == "break" else {}),
        })

    cleaned.sort(key=lambda r: (r["day"], _mins(r["start"])))
    n = 1
    for row in cleaned:
        if row["kind"] == "round":
            row["round"] = n
            n += 1
    return cleaned


def day_dates(tournament) -> list[str]:
    """The actual calendar date of each day, so the schedule can show
    "Saturday 14 November" rather than "Day 1"."""
    if not tournament.event_date:
        return []
    return [(tournament.event_date + timedelta(days=i)).isoformat()
            for i in range(max(1, tournament.days or 1))]


def round_slot(tournament, round_no: int) -> Optional[dict]:
    """When a given round is scheduled, for showing next to its pairings."""
    for row in (tournament.schedule or []):
        if row.get("kind") == "round" and row.get("round") == round_no:
            return row
    return None
