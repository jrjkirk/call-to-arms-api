"""Render the weekly league rankings as a PNG and post it to Discord.

Entry point for the league-rankings-image GitHub Actions workflow (weekly,
Thursdays). Reuses main.py's _compute_league_rankings helper directly (same
DRY pattern as post_pairings_image.py importing from admin.py) so this always
reflects the same rankings/W-D-L/most-played-faction data the live site shows.

Iterates every active, leagues-enabled club rather than resolving a single
active club — league rankings is a league-only feature, so only clubs with
leagues_enabled=true are ever considered (same gating as the Discord
Integrations webhooks panel). The webhook is resolved DB-only via
resolve_webhook_url with no env-var fallback (matching run_call_to_arms_check.py
and the signups.py read-path convention); a club with no configured
league_rankings webhook is skipped cleanly, before its rankings are computed.
"""
import json

import httpx
from sqlmodel import Session, select

from database import engine, resolve_webhook_url
from main import _compute_league_rankings
from models import Club
from render_league_rankings_image import render_league_rankings_image


def main() -> None:
    # Resolve every qualifying club's webhook + rankings inside the DB session,
    # then close it before doing any network posting (same ordering the
    # single-club version deliberately used — never hold a DB connection open
    # across a 30s Discord POST).
    jobs: list[tuple[str, str, list]] = []  # (slug, webhook_url, rankings)
    with Session(engine) as db:
        clubs = db.exec(
            select(Club).where(Club.active == True).where(Club.leagues_enabled == True)
        ).all()
        if not clubs:
            print("No active leagues-enabled clubs, nothing to post.")
            return

        for club in clubs:
            webhook_url = resolve_webhook_url(db, club.id, "league_rankings")
            if not webhook_url:
                # Skip loudly-but-cleanly, before computing rankings for a club
                # that has nowhere to post them.
                print(f"[{club.slug}] No league-rankings webhook configured, skipping.")
                continue
            rankings = _compute_league_rankings(db, club.id)
            jobs.append((club.slug, webhook_url, rankings))

    for slug, webhook_url, rankings in jobs:
        buf = render_league_rankings_image(rankings)
        if buf is None:
            print(f"[{slug}] No league results yet, skipping.")
            continue

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
        print(f"[{slug}] Posted league rankings ({resp.status_code}).")


if __name__ == "__main__":
    main()
