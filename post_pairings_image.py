"""Render the weekly pairings as a PNG and post it to the relevant Discord webhook.

post_pairings_image_for(db, system, week) — importable helper used by the
auto-pairings scheduler.  Returns True if the image was posted, False if
there were no pairings rows to render.

The __main__ entry point (invoked by the post-pairings-image GitHub Actions
workflow) delegates to this function using SYSTEM/WEEK env vars.
"""
import json
import os

import httpx
from sqlmodel import Session, select

from database import engine
from models import Pairing
from admin import _collect_signups_for_rows, _pairing_rows_to_display
from render_pairings_image import render_pairings_image

WEBHOOK_MAP = {
    "The Old World": os.environ.get("DISCORD_TOW_PAIRINGS_WEBHOOK_URL", ""),
    "The Horus Heresy": os.environ.get("DISCORD_HH_PAIRINGS_WEBHOOK_URL", ""),
    "Kill Team": os.environ.get("DISCORD_KT_PAIRINGS_WEBHOOK_URL", ""),
}


def post_pairings_image_for(db: Session, system: str, week: str) -> bool:
    """Render pairings for (system, week) and post to Discord.

    Returns True if the image was posted, False if no pairing rows were found.
    The db session is used read-only here; no commits are made.
    """
    webhook_url = WEBHOOK_MAP.get(system, "")
    if not webhook_url:
        print(f"No pairings webhook configured for {system!r}, skipping.")
        return False

    rows = db.exec(
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .order_by(Pairing.id)
    ).all()

    signups_by_id = _collect_signups_for_rows(rows, db)
    display_rows = _pairing_rows_to_display(rows, signups_by_id, system)

    buf = render_pairings_image(display_rows, week, system)
    if buf is None:
        print(f"No pairings found for {system!r} week {week!r}, skipping.")
        return False

    content = f"📋 **{system} — Pairings for {week}**"
    resp = httpx.post(
        webhook_url,
        data={"payload_json": json.dumps({"content": content})},
        files={"file": ("pairings.png", buf, "image/png")},
        timeout=30,
    )
    print(f"Posted pairings image for {system!r} {week!r} ({resp.status_code}).")
    return True


def main() -> None:
    system = os.environ["SYSTEM"]
    week = os.environ["WEEK"]
    with Session(engine) as db:
        post_pairings_image_for(db, system, week)


if __name__ == "__main__":
    main()
