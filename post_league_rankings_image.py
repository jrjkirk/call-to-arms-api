"""Render the weekly league rankings as a PNG and post it to Discord.

Entry point for the league-rankings-image GitHub Actions workflow (weekly,
Thursdays). Reuses main.py's GET /league/rankings logic directly (same DRY
pattern as post_pairings_image.py importing from admin.py) so this always
reflects the same rankings/W-D-L/most-played-faction data the live site shows.
"""
import json
import os

import httpx
from sqlmodel import Session

from database import engine
from main import league_rankings
from render_league_rankings_image import render_league_rankings_image

DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL = os.environ.get("DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL", "")


def main() -> None:
    if not DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL:
        print("DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL not set, skipping.")
        return

    with Session(engine) as db:
        rankings = league_rankings(_=None, session=db)

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
        DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL,
        data={"payload_json": json.dumps({"content": content})},
        files={"file": ("league_rankings.png", buf, "image/png")},
        timeout=30,
    )
    print(f"Posted league rankings ({resp.status_code}).")


if __name__ == "__main__":
    main()