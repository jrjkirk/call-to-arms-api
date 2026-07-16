"""Manual/fallback entry point: post The Old World Call to Arms to the
DISCORD_CALL_TO_ARMS_WEBHOOK_URL env webhook, for one-off runs via the
"Weekly TOW Call to Arms (manual fallback)" workflow.

The scheduled, club-aware path is run_call_to_arms_check.py. All message
content (templates + mission selection + image) lives in
call_to_arms_content.py — this wrapper just posts that system's default
template to the env webhook. It does not apply a club's edited template
(no DB context); the scheduled path does.
"""
import os
from datetime import date, timedelta

import call_to_arms_content as cta_content

SYSTEM = "The Old World"


def next_wednesday(from_date: date | None = None) -> date:
    if from_date is None:
        from_date = date.today()
    days_ahead = (2 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


if __name__ == "__main__":
    webhook = os.environ.get("DISCORD_CALL_TO_ARMS_WEBHOOK_URL", "")
    app_url = os.environ.get("APP_PUBLIC_URL", "")
    cta_content.post(webhook, cta_content.default_template(SYSTEM), SYSTEM, next_wednesday(), app_url)
