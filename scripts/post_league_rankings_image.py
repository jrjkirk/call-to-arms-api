"""Render the weekly league rankings as a PNG and post it to Discord.

Entry point for the league-rankings-image GitHub Actions workflow (weekly,
Thursdays). Reuses main.py's _compute_league_rankings helper directly (same
DRY pattern as post_pairings_image.py importing from admin.py) so this always
reflects the same rankings/W-D-L/most-played-faction data the live site shows.

Iterates every league-enabled ClubSystem row rather than resolving a single
active club — a club can run more than one system's league, each posted
separately (league_rankings is a per-system webhook — see admin.py's
WEBHOOK_TYPES_LEAGUE). The webhook is resolved DB-only via
resolve_webhook_url with no env-var fallback (matching run_call_to_arms_check.py
and the signups.py read-path convention); a club-system with no configured
league_rankings webhook is skipped cleanly, before its rankings are computed.
"""
import json

import httpx
from sqlmodel import Session, select

from database import posting_enabled, engine, resolve_webhook_url
from league import _current_season_id
from main import _compute_league_rankings
from models import Club, ClubSystem, SystemConfig
# Qualified with the package name, not a bare sibling import: these modules
# are imported BOTH as scripts (python scripts/x.py, where scripts/ lands on
# sys.path) and as a package (scheduler.py, where it does not). Only the
# qualified form works in both, and PYTHONPATH=. — which CLAUDE.md requires
# and every workflow sets — is what makes it resolve for the script case.
from scripts.render_league_rankings_image import render_league_rankings_image


def league_club_systems(db: Session):
    """Every (club_system, club, system_config) with a league switched on."""
    return db.exec(
        select(ClubSystem, Club, SystemConfig)
        .join(Club, Club.id == ClubSystem.club_id)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(Club.active == True)
        .where(ClubSystem.league_enabled == True)
    ).all()


def collect_job(db: Session, club: Club, system_config: SystemConfig):
    """Resolve one club-system's webhook and rankings, or None if it can't post.

    Split from the sending half so callers can finish every database read and
    close the session BEFORE any Discord POST. That ordering is deliberate:
    database.py pools against a Supabase ceiling of ~16 connections, and a 30s
    upload is a long time to hold one. See KNOWN_ISSUES.md.
    """
    label = f"{club.slug}/{system_config.slug}"
    # league_rankings is a per-system webhook type (see admin.py's
    # WEBHOOK_TYPES_LEAGUE) — a club running two leagues can route each one's
    # rankings post to its own Discord channel.
    if not posting_enabled(db, club.id, system_config.legacy_system_name, "league"):
        print(f"[{label}] League posts switched off, skipping.")
        return None
    webhook_url = resolve_webhook_url(db, club.id, "league_rankings", system_config.id)
    if not webhook_url:
        # Skip loudly-but-cleanly, before computing rankings for a club-system
        # that has nowhere to post them.
        print(f"[{label}] No league-rankings webhook configured, skipping.")
        return None
    season_id = _current_season_id(db, club.id, system_config.id)
    if season_id is None:
        print(f"[{label}] No season configured yet, skipping.")
        return None
    rankings = _compute_league_rankings(db, club.id, system_config.id, season_id)
    return (club.slug, system_config.name, webhook_url, rankings)


def send_job(job) -> bool:
    """Render and post one collected job. No database access — see collect_job.

    Returns False when there is nothing to render (no results yet), so a caller
    tracking "has this week's post gone out" doesn't record a post that never
    happened.
    """
    slug, system_name, webhook_url, rankings = job
    buf = render_league_rankings_image(rankings)
    if buf is None:
        print(f"[{slug}] No league results yet, skipping.")
        return False

    content = (
        f"📜 **The {system_name} League Standings** 📜\n\n"
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
    # Checked, not just printed. Without this a 404 from a deleted webhook
    # logged "Posted league rankings (404)." and the job went green.
    resp.raise_for_status()
    print(f"[{slug}] Posted league rankings ({resp.status_code}).")
    return True


def main() -> None:
    """Post every league-enabled club-system's standings, right now.

    Unconditional on purpose. This is the manual override — the schedule, its
    dedup and its catch-up all live in run_league_rankings_check.py, and an
    override that consulted them would refuse to do the one thing it is for.
    It deliberately records nothing either, so using it never makes the
    scheduled post think it has already gone out.
    """
    jobs = []
    with Session(engine) as db:
        rows = league_club_systems(db)
        if not rows:
            print("No active clubs with a league enabled, nothing to post.")
            return
        for _club_system, club, system_config in rows:
            job = collect_job(db, club, system_config)
            if job:
                jobs.append(job)

    for job in jobs:
        send_job(job)


if __name__ == "__main__":
    main()
