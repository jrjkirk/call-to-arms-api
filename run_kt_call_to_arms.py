"""Manual/fallback entry point: post the Kill Team Call to Arms to the
DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL env webhook, for one-off runs via the
"Weekly Kill Team Call to Arms (manual fallback)" workflow.

The scheduled, club-aware path is run_call_to_arms_check.py; message content
lives in call_to_arms_content.py. The KT default template has no session
date, so today's date is passed as a harmless unused value.
"""
import os
from datetime import date, timedelta

import call_to_arms_content as cta_content

SYSTEM = "Kill Team"


def next_friday(from_date: date | None = None) -> date:
    if from_date is None:
        from_date = date.today()
    days_ahead = (4 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


if __name__ == "__main__":
    webhook = os.environ.get("DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL", "")
    app_url = os.environ.get("APP_PUBLIC_URL", "")
    cta_content.post(webhook, cta_content.default_template(SYSTEM), SYSTEM, next_friday(), app_url)
