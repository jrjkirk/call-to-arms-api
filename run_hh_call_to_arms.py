"""Manual/fallback entry point: post The Horus Heresy Call to Arms to the
DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL env webhook, for one-off runs via the
"Weekly Horus Heresy Call to Arms (manual fallback)" workflow. Skips on
off-weeks (HH is fortnightly, anchored on HH_SESSION_ANCHOR).

The scheduled, club-aware path is run_call_to_arms_check.py; message content
lives in call_to_arms_content.py. HH_SESSION_ANCHOR is also imported by
seed_clubs.py, so it stays defined here.
"""
import os
from datetime import date, timedelta

import call_to_arms_content as cta_content

SYSTEM = "The Horus Heresy"
HH_SESSION_ANCHOR = date(2026, 5, 8)


def hh_next_session_friday(d: date | None = None) -> date:
    if d is None:
        d = date.today()
    if d <= HH_SESSION_ANCHOR:
        return HH_SESSION_ANCHOR
    delta_days = (d - HH_SESSION_ANCHOR).days
    fortnights_passed = delta_days // 14
    candidate = HH_SESSION_ANCHOR + timedelta(days=fortnights_passed * 14)
    if d > candidate:
        candidate += timedelta(days=14)
    return candidate


def is_hh_session_week(d: date | None = None) -> bool:
    if d is None:
        d = date.today()
    next_session = hh_next_session_friday(d)
    days_until = (next_session - d).days
    return 0 <= days_until <= 6


if __name__ == "__main__":
    if not is_hh_session_week(date.today()):
        print("Not an HH session week, skipping.")
    else:
        webhook = os.environ.get("DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL", "")
        app_url = os.environ.get("APP_PUBLIC_URL", "")
        cta_content.post(webhook, cta_content.default_template(SYSTEM), SYSTEM, hh_next_session_friday(), app_url)
