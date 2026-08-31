"""In-process scheduler for the four periodic jobs.

Why this exists
---------------
These jobs ran as GitHub Actions crons on `0 * * * *`. GitHub does not honour
that: on 30 Aug 2026 the call-to-arms check ran five times, not twenty-four —
01:58, 07:23, 13:32, 18:09, 21:27 — and The Old World at EGNWGC lost a week's
post because its fire window was 12:00-13:30 and the run landed at 13:32.

So the tick moved here, into the process that is already running. It shares the
app's SQLAlchemy engine deliberately: `database.py` pools 10+4 against a
Supabase ceiling of 16, so a scheduler in a separate process would have opened
a second pool and starved the web app. Sharing costs nothing.

The jobs are blocking SQLAlchemy calls, so each one runs in a worker thread
rather than on the event loop, where it would stall request serving.

Safety
------
Off unless SCHEDULER_ENABLED is set. That default is not politeness: local
.env points at a real database holding real Discord webhooks, so a loop that
started by default would make `uvicorn main:app --reload` post to a live club
channel. Set it as a Fly secret and nowhere else.

Every tick claims its (job, period) first — see database.claim_job_period. One
machine runs today, but `fly deploy` briefly overlaps processes and a second
machine would double every post.

A crashed winner leaves its period claimed and nothing runs for another tick.
That is only survivable because the fire windows in week_logic.py now run to
the end of the configured day; don't narrow them again without revisiting this.
"""
import asyncio
import os
from datetime import datetime, timezone

from sqlmodel import Session

from database import claim_job_period, engine, prune_job_claims
from observability import capture

TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "300"))


def enabled() -> bool:
    return (os.environ.get("SCHEDULER_ENABLED", "") or "").strip().lower() not in ("", "0", "false")


def _jobs() -> dict:
    """Imported lazily so this module stays importable (and the app bootable)
    even if a job script has a problem — and so a machine with the scheduler
    switched off never pays for importing four scripts it will not run."""
    from scripts import (
        run_auto_pairings_check,
        run_call_outs_check,
        run_call_to_arms_check,
        run_table_booking_cutoff_check,
    )
    return {
        "call_to_arms_check": run_call_to_arms_check.main,
        "auto_pairings_check": run_auto_pairings_check.main,
        "call_outs_check": run_call_outs_check.main,
        "table_booking_cutoff_check": run_table_booking_cutoff_check.main,
    }


def period_key(now: datetime | None = None) -> str:
    """The tick bucket, in UTC, floored to TICK_SECONDS.

    Two runners a few seconds apart must agree on the bucket or both would
    claim and both would work, so this floors rather than rounds and works in
    whole seconds since the epoch.
    """
    now = now or datetime.now(timezone.utc)
    bucket = int(now.timestamp()) // TICK_SECONDS * TICK_SECONDS
    # Seconds are in the format on purpose. Formatting to the minute silently
    # collapsed every bucket inside one minute into the same key, so any tick
    # shorter than 60s claimed once and then did nothing — which is exactly
    # what a shortened SCHEDULER_TICK_SECONDS would be set to test.
    return datetime.fromtimestamp(bucket, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _run_one(name: str, fn) -> None:
    """Claim, then run. Separate sessions on purpose: claim_job_period commits
    and rolls back on a lost race, so it must not share a session with anything
    else."""
    period = period_key()
    with Session(engine) as db:
        if not claim_job_period(db, name, period):
            return
    fn()


async def tick_loop() -> None:
    jobs = _jobs()
    print(f"[scheduler] started — {len(jobs)} jobs every {TICK_SECONDS}s")
    while True:
        for name, fn in jobs.items():
            try:
                await asyncio.to_thread(_run_one, name, fn)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # One bad job must never take the loop down with it, or a
                # single broken club silences every other club's posts.
                capture(e, kind="scheduler_job", system=name)
        try:
            await asyncio.to_thread(_prune)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            capture(e, kind="scheduler_prune")
        await asyncio.sleep(TICK_SECONDS)


def _prune() -> None:
    """Hourly, not every tick — four jobs on a five-minute tick is about 1,150
    claim rows a day, worth clearing but not worth a DELETE every five minutes."""
    if datetime.now(timezone.utc).minute >= TICK_SECONDS // 60:
        return
    with Session(engine) as db:
        n = prune_job_claims(db)
        db.commit()
        if n:
            print(f"[scheduler] pruned {n} stale claim rows")
