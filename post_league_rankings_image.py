"""Render the weekly league rankings as a PNG and post it to Discord.

Entry point for the league-rankings-image GitHub Actions workflow (weekly,
Thursdays). Reuses main.py's _compute_league_rankings helper directly (same
DRY pattern as post_pairings_image.py importing from admin.py) so this always
reflects the same rankings/W-D-L/most-played-faction data the live site shows.
"""
import json
import os

import httpx
from sqlmodel import Session, select

from database import engine, resolve_webhook_url
from models import Club
from main import _compute_league_rankings
from render_league_rankings_image import render_league_rankings_image

DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL = os.environ.get("DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL", "")


def _resolve_single_active_club_id(db: Session) -> int:
    """Resolve the one active club, for this script's entry point — rankings
    have no per-system dimension, so unlike post_pairings_image.py's
    _resolve_single_club_id there's no system to key off. Raises rather than
    guessing if that's ever not true, so a second active club fails loudly
    here instead of silently posting the wrong club's rankings."""
    clubs = db.exec(select(Club).where(Club.active == True)).all()
    if len(clubs) != 1:
        raise RuntimeError(
            f"Cannot resolve a single active club — found {len(clubs)}, expected exactly 1. "
            f"This script has no club selector; needs a real design decision once a second "
            f"active club exists."
        )
    return clubs[0].id


def main() -> None:
    with Session(engine) as db:
        club_id = _resolve_single_active_club_id(db)

        webhook_url = resolve_webhook_url(db, club_id, "league_rankings") or DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL
        if not webhook_url:
            print("No league-rankings webhook configured, skipping.")
            return

        rankings = _compute_league_rankings(db, club_id)

    buf = render_league_rankings_image(rankings)
    if buf is None:
        print("No league results yet, skipping.")
        return

    content = (
        "📜 **The Old World League Standings** 📜\n\n"
        "The latest rankings have been recorded by the keepers of the chronicle. "
        "View who climbs, who falls, and who clings to the top of the table.\n\n"
        "*Submit your results to keep the standings sharp. The throne is never safe.*"
    )
    resp = httpx.post(
        webhook_url,
        data={"payload_json": json.dumps({"content": content})},
        files={"file": ("league_rankings.png", buf, "image/png")},
        timeout=30,
    )
    print(f"Posted league rankings ({resp.status_code}).")


if __name__ == "__main__":
    main()