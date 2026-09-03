"""League-standings check — runs both in-process (scheduler.py, every 5
minutes) and on the league-rankings-check GitHub Actions workflow.

Why this exists
---------------
The standings post used to be a single `cron: '0 19 * * 4'` workflow: Thursday
19:00 UTC, no settings, no dedup, no catch-up. A weekly cron has exactly one
slot, and GitHub honoured none of them. Observed run times against a 20:00 BST
target: 21:00, 21:09, 21:12, 21:17, 21:44, 21:54, 22:22 — and twice it slipped
past midnight and posted the league table at 01:39 and 03:24 on a Friday, when
nobody was looking. Miss the slot outright and the week's post is simply gone,
because nothing recorded that one had been due.

So the decision moved here, onto the reliable five-minute tick, and the club
picks its own day and time. Same split as run_auto_pairings_check.py: the part
that decides is cheap and runs anywhere, and only the rendering — which needs
matplotlib, absent from the 256 MB API image — is handed to a GitHub runner via
workflow_dispatch, which queues immediately rather than whenever GitHub feels
like it.

What is configurable, and what isn't
------------------------------------
Day and time are per club-system, set in the admin League tab. Whether the club
posts standings at all is NOT a setting here — that is the existing `league`
posting switch (shared with results and achievements, on the Discord tab),
because one decision deserves one switch.

One club/system failing does not stop the others.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session

from database import (
    engine,
    get_setting as _get_setting,
    posting_enabled,
    record_job_run,
    resolve_webhook_url,
    system_setting_slug as _slug,
    upsert_setting as _upsert_setting,
)
from github_dispatch import LEAGUE_RANKINGS_WORKFLOW, dispatch_enabled, dispatch_workflow
from week_logic import _is_league_rankings_due

JOB_NAME = "league_rankings_check"

DEFAULT_DAY = "Thursday"
# 20:00 UK, which is what the old `0 19 * * 4` cron meant for eight months of
# the year. Under GMT that cron was 19:00 local; picking one of the two, the
# summer reading is the one clubs actually experienced.
DEFAULT_TIME = "20:00"


def settings_for(db: Session, club_id: int, system: str) -> dict:
    """This club-system's standings schedule, with the old cron as the default
    so a club that never opens the new panel keeps the behaviour it had."""
    slug = _slug(system)
    return {
        "day": _get_setting(db, club_id, f"league_rankings_{slug}_day", DEFAULT_DAY) or DEFAULT_DAY,
        "time": _get_setting(db, club_id, f"league_rankings_{slug}_time", DEFAULT_TIME) or DEFAULT_TIME,
        "last_posted": _get_setting(db, club_id, f"league_rankings_{slug}_last_posted", None),
        "post_requested": _get_setting(db, club_id, f"league_rankings_{slug}_post_requested", None),
    }


def _load_sender():
    """Return post_league_rankings_image's collect/send pair, or None here.

    It pulls in matplotlib via render_league_rankings_image, which the API
    container does not install — so a failed import is how this process learns
    it must ask a runner instead. Lazy for that reason: at module scope it would
    make this file unimportable inside the API, which is exactly what kept the
    old job stranded on GitHub Actions.
    """
    try:
        from scripts.post_league_rankings_image import collect_job, league_club_systems, send_job

        return collect_job, league_club_systems, send_job
    except ImportError as exc:
        print(f"[league-rankings] no local renderer ({exc}) — will ask a GitHub runner")
        return None


def _league_club_systems(db: Session):
    """The same query the poster uses, imported without the renderer attached.

    Duplicated deliberately rather than imported from post_league_rankings_image:
    that module drags matplotlib in, and this process needs the list even when
    it cannot render a thing.
    """
    from sqlmodel import select

    from models import Club, ClubSystem, SystemConfig

    return db.exec(
        select(ClubSystem, Club, SystemConfig)
        .join(Club, Club.id == ClubSystem.club_id)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(Club.active == True)
        .where(ClubSystem.league_enabled == True)
    ).all()


def main() -> list[str]:
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    today_key = now_uk.date().isoformat()
    print(f"League-rankings check — {now_uk.strftime('%Y-%m-%d %H:%M %Z')}")

    errors: list[str] = []
    sender = _load_sender()
    due: list[tuple[int, str, str]] = []   # (club_id, system, label)
    jobs: list[tuple] = []

    # Pass 1: decide. Pure database work, so it runs anywhere.
    with Session(engine) as db:
        rows = _league_club_systems(db)
        if not rows:
            print("No active clubs with a league enabled.")
        for _club_system, club, system_config in rows:
            system = system_config.legacy_system_name
            label = f"{club.slug}/{system_config.slug}"
            try:
                # Checked HERE as well as in collect_job, not instead of it.
                # collect_job runs on the runner; this runs on the tick that
                # decides whether to summon one, and a club with standings
                # switched off must not have a workflow queued on its behalf
                # every week for a post that will never be sent.
                if not posting_enabled(db, club.id, system, "league"):
                    print(f"[{label}] SKIP — league posts switched off")
                    continue
                if not resolve_webhook_url(db, club.id, "league_rankings", system_config.id):
                    print(f"[{label}] SKIP — no league-rankings webhook configured")
                    continue

                settings = settings_for(db, club.id, system)
                if not _is_league_rankings_due(settings, now_uk, today_key):
                    print(
                        f"[{label}] SKIP — not due (day={settings['day']}, "
                        f"time={settings['time']}, last_posted={settings['last_posted']!r}, "
                        f"today={today_key})"
                    )
                    continue
                due.append((club.id, system, label))
            except Exception as exc:
                import traceback
                print(f"[{label}] ERROR — {exc}")
                traceback.print_exc()
                errors.append(f"{label}: {exc}")

        # Nothing to render here: ask a runner once, and let its own pass do it.
        if due and sender is None:
            _request_run(db, due, today_key)
            record_job_run(db, JOB_NAME, "error" if errors else "ok",
                           "; ".join(errors[:5]) if errors else None)
            db.commit()
            return errors

        # Pass 2a: collect everything the sender needs, still inside the session.
        if due:
            collect_job, _list, _send = sender
            by_key = {(c.id, sc.legacy_system_name): (c, sc) for _cs, c, sc in rows}
            for club_id, system, label in due:
                club, system_config = by_key[(club_id, system)]
                try:
                    job = collect_job(db, club, system_config)
                    if job:
                        jobs.append((club_id, system, label, job))
                except Exception as exc:
                    import traceback
                    print(f"[{label}] ERROR — {exc}")
                    traceback.print_exc()
                    errors.append(f"{label}: {exc}")

    # Pass 2b: render and post with the session closed — a 30s Discord upload
    # must never hold one of the pool's ~16 connections. Same ordering the
    # poster has always used.
    posted: list[tuple[int, str]] = []
    if jobs:
        _collect, _list, send_job = sender
        for club_id, system, label, job in jobs:
            try:
                if send_job(job):
                    posted.append((club_id, system))
            except Exception as exc:
                import traceback
                print(f"[{label}] ERROR — {exc}")
                traceback.print_exc()
                errors.append(f"{label}: {exc}")

    with Session(engine) as db:
        for club_id, system in posted:
            _upsert_setting(db, club_id, f"league_rankings_{_slug(system)}_last_posted", today_key)
        record_job_run(db, JOB_NAME, "error" if errors else "ok",
                       "; ".join(errors[:5]) if errors else None)
        db.commit()

    return errors


def _request_run(db: Session, due: list[tuple[int, str, str]], today_key: str) -> None:
    """Ask a GitHub runner to do today's posts, at most once per club-system.

    The request marker is what stops a five-minute tick queueing a workflow run
    every five minutes for the rest of the day when something downstream is
    broken. If the dispatched run fails, the workflow's own cron still sweeps
    the same check later — a failed dispatch costs promptness, not the post.
    """
    wanted = [
        (club_id, system, label)
        for club_id, system, label in due
        if _get_setting(db, club_id, f"league_rankings_{_slug(system)}_post_requested") != today_key
    ]
    if not wanted:
        return
    ok, reason = dispatch_workflow(LEAGUE_RANKINGS_WORKFLOW)
    if not ok:
        if dispatch_enabled():
            # Configured but failing is worth saying; never configured is a
            # deployment choice, and the cron backstop still covers it.
            print(f"[league-rankings] could not queue the post — {reason}")
        return
    for club_id, system, label in wanted:
        _upsert_setting(db, club_id, f"league_rankings_{_slug(system)}_post_requested", today_key)
        print(f"[{label}] asked a GitHub runner to post today's standings")


if __name__ == "__main__":
    # Non-zero exit on failure, so the Actions tab is an honest health signal
    # rather than green whatever happened. In the __main__ guard, not in main():
    # scheduler.py calls main() directly, and a SystemExit raised there would go
    # straight through its `except Exception` and stop the tick loop.
    _errors = main()
    if _errors:
        print(f"{JOB_NAME}: {len(_errors)} failure(s)")
        raise SystemExit(1)
