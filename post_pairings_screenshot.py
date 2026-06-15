"""Entry point for GitHub Actions: screenshot the public /pairings page for a
given system/week and post it to the relevant Discord webhook.

Manually triggered (workflow_dispatch) for now — system/week provided as
inputs. Designed to also be called by the hourly auto-pairings scheduler once
that's built, and by the admin "Post to Discord" button via the GitHub API.

No database access — screenshots the live public page, which already renders
matchups exactly as players see them (real faction icons, accent colours).
"""
import json
import os
from urllib.parse import urlencode

import httpx
from playwright.sync_api import sync_playwright

SYSTEM = os.environ["SYSTEM"]
WEEK = os.environ["WEEK"]
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "https://www.calltoarms.app")

WEBHOOK_MAP = {
    "The Old World": os.environ.get("DISCORD_TOW_PAIRINGS_WEBHOOK_URL", ""),
    "The Horus Heresy": os.environ.get("DISCORD_HH_PAIRINGS_WEBHOOK_URL", ""),
    "Kill Team": os.environ.get("DISCORD_KT_PAIRINGS_WEBHOOK_URL", ""),
}


def main() -> None:
    webhook_url = WEBHOOK_MAP.get(SYSTEM, "")
    if not webhook_url:
        print(f"No pairings webhook configured for {SYSTEM!r}, skipping.")
        return

    params = urlencode({"system": SYSTEM, "week": WEEK})
    url = f"{APP_PUBLIC_URL}/pairings?{params}"
    print(f"Loading {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".matchups, .empty-state", timeout=20000)

        if page.locator(".matchups").count() == 0:
            print("No published matchups for this week/system, skipping.")
            browser.close()
            return

        # let any entrance animation settle before capturing
        page.wait_for_timeout(2000)
        screenshot_bytes = page.locator(".matchups").screenshot()
        browser.close()

    content = f"📋 **{SYSTEM} — Pairings for {WEEK}**"
    files = {"file": ("pairings.png", screenshot_bytes, "image/png")}
    resp = httpx.post(
        webhook_url,
        data={"payload_json": json.dumps({"content": content})},
        files=files,
        timeout=30,
    )
    print(f"Posted pairings screenshot ({resp.status_code}).")


if __name__ == "__main__":
    main()