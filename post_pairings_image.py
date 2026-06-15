"""Entry point for GitHub Actions: render the weekly pairings as a PNG
(matplotlib) and post it to the relevant Discord webhook.

Manually triggered (workflow_dispatch) or via the admin "Post to Discord"
button dispatching post-pairings-image.yml. Reads SYSTEM and WEEK from
environment variables. Requires DATABASE_URL to connect to Postgres.
"""
import json
import os

import httpx
from sqlmodel import Session, select

from database import engine
from models import Pairing
from admin import _collect_signups_for_rows, _pairing_rows_to_display
from render_pairings_image import render_pairings_image

SYSTEM = os.environ["SYSTEM"]
WEEK = os.environ["WEEK"]

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

    with Session(engine) as db:
        rows = db.exec(
            select(Pairing)
            .where(Pairing.week == WEEK)
            .where(Pairing.system == SYSTEM)
            .order_by(Pairing.id)
        ).all()

        signups_by_id = _collect_signups_for_rows(rows, db)
        display_rows = _pairing_rows_to_display(rows, signups_by_id, SYSTEM)

    buf = render_pairings_image(display_rows, WEEK, SYSTEM)
    if buf is None:
        print(f"No pairings found for {SYSTEM!r} week {WEEK!r}, skipping.")
        return

    content = f"📋 **{SYSTEM} — Pairings for {WEEK}**"
    resp = httpx.post(
        webhook_url,
        data={"payload_json": json.dumps({"content": content})},
        files={"file": ("pairings.png", buf, "image/png")},
        timeout=30,
    )
    print(f"Posted pairings image ({resp.status_code}).")


if __name__ == "__main__":
    main()
