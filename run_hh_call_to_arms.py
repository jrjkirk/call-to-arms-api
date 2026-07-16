"""Entry point for GitHub Actions: post the fortnightly Horus Heresy Call to Arms.

Mirrors the original's run_scheduled_hh_call_to_arms() / post_hh_call_to_arms().
Skips silently on off-weeks (HH runs fortnightly, anchored on HH_SESSION_ANCHOR —
same anchor and fortnight math already ported to the frontend's weekIdForSystem).
No database access needed.
"""
import os
from datetime import date, timedelta
import httpx

DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL = os.environ.get("DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL", "")
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")

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


def post_hh_call_to_arms(webhook_url: str | None = None, app_url: str | None = None) -> None:
    """webhook_url/app_url default to this module's env vars for the
    __main__ manual-run path; the scheduler passes a resolved per-club
    webhook instead."""
    webhook = webhook_url or DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL
    if not webhook:
        print("No HH call-to-arms webhook, skipping.")
        return

    signup_url = app_url or APP_PUBLIC_URL or "https://your-app-url"
    content = (
        "⚔️ **The Horus Heresy — Call to Arms** ⚔️\n\n"
        "*\"In the long shadow of the Emperor's wrath, brothers turn against brothers. "
        "The galaxy burns, and the loyal and the lost alike must answer the call to war.\"*\n\n"
        f"Friday's gathering approaches.  Sign up here: {signup_url}"
    )

    try:
        httpx.post(webhook, json={"content": content}, timeout=10)
        print("Posted HH Call to Arms.")
    except Exception as e:
        print(f"Failed to post HH Call to Arms: {e}")


if __name__ == "__main__":
    if not is_hh_session_week(date.today()):
        print("Not an HH session week, skipping.")
    else:
        post_hh_call_to_arms()