"""Render the weekly pairings as a PNG and post it to the relevant Discord webhook.

post_pairings_image_for(db, system, week, club_id) — importable helper used
by the auto-pairings scheduler.  Returns True if the image was posted,
False if there were no pairings rows to render.

The __main__ entry point (invoked by the post-pairings-image GitHub Actions
workflow) delegates to this function using SYSTEM/WEEK env vars. That
workflow is a manual workflow_dispatch (a human picks system+week, no
per-club input) — see _resolve_single_club_id below for how club_id is
resolved for that entry point specifically.
"""
import json
import os

import httpx
from sqlmodel import Session, select

from database import engine, resolve_webhook_url, scoped
from models import ClubSystem, Pairing, SystemConfig
from admin import _collect_signups_for_rows, _pairing_rows_to_display
from render_pairings_image import render_pairings_image


def post_pairings_image_for(db: Session, system: str, week: str, club_id: int) -> bool:
    """Render pairings for (system, week, club_id) and post to Discord.

    Returns True if the image was posted, False if no pairing rows were found.
    The db session is used read-only here; no commits are made.
    """
    system_config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    system_id = system_config.id if system_config else None
    webhook_url = resolve_webhook_url(db, club_id, "pairings", system_id)
    if not webhook_url:
        print(f"No pairings webhook configured for {system!r}, skipping.")
        return False

    rows = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .order_by(Pairing.id)
    ).all()

    signups_by_id = _collect_signups_for_rows(rows, db, club_id)
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


def _resolve_single_club_id(db: Session, system: str) -> int:
    """Resolve the one club running `system`, for the manual workflow_dispatch
    entry point below — that workflow has no club selector, so this only
    works while every system has exactly one club_systems row. Raises
    rather than guessing if that's ever not true, so a second club sharing
    a system fails loudly here instead of silently posting the wrong
    club's pairings; picking a real club selector for this workflow is a
    separate design decision, not made here."""
    system_config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    if system_config is None:
        raise RuntimeError(f"No SystemConfig row for legacy_system_name={system!r}")

    club_systems = db.exec(
        select(ClubSystem).where(ClubSystem.system_id == system_config.id)
    ).all()
    if len(club_systems) != 1:
        raise RuntimeError(
            f"Cannot resolve a single club for {system!r} — found "
            f"{len(club_systems)} club_systems row(s), expected exactly 1. "
            f"This manual workflow has no club selector; needs a real design "
            f"decision once a second club runs this system."
        )
    return club_systems[0].club_id


def main() -> None:
    system = os.environ["SYSTEM"]
    week = os.environ["WEEK"]
    with Session(engine) as db:
        club_id = _resolve_single_club_id(db, system)
        post_pairings_image_for(db, system, week, club_id=club_id)


if __name__ == "__main__":
    main()
