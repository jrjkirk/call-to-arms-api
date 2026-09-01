"""Backfill `auto_pairings_<slug>_posted_week` from `auto_pairings_<slug>_last_week`.

Why this is needed exactly once
-------------------------------
The auto-pairings job used to do "pair" and "post the image" as one step, so
`last_week` was the only marker: it meant both "pairings exist for this week"
and, implicitly, "the image went out". Splitting the two (so pairing can run on
the reliable in-process tick while rendering stays on a GitHub runner) needs a
second marker for the post.

Without this backfill, the first run after deploy sees `posted_week` unset for
every club/system, decides the image is outstanding, and posts a **second copy
of pairings that already went to Discord** — for every club paired in the
current week. The blast radius is "every club that paired since Monday", which
is the worst possible first impression of a reliability fix.

Setting posted_week = last_week says "whatever has been paired has been
posted", which is true of every row written by the old one-step job.

Idempotent: rows that already have a posted_week are left alone, so running it
twice (or after the new job has started writing its own markers) is harmless.

    PYTHONPATH=. python migrations/backfill_auto_pairings_posted_week.py
    PYTHONPATH=. python migrations/backfill_auto_pairings_posted_week.py --dry-run
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import ClubSetting

LAST_SUFFIX = "_last_week"
POSTED_SUFFIX = "_posted_week"
PREFIX = "auto_pairings_"


def main(dry_run: bool = False) -> None:
    with Session(engine) as db:
        rows = db.exec(select(ClubSetting)).all()

        existing = {
            (r.club_id, r.key) for r in rows
        }
        last_week_rows = [
            r for r in rows
            if r.key.startswith(PREFIX) and r.key.endswith(LAST_SUFFIX) and r.value
        ]

        written = skipped = 0
        for r in last_week_rows:
            posted_key = r.key[: -len(LAST_SUFFIX)] + POSTED_SUFFIX
            if (r.club_id, posted_key) in existing:
                skipped += 1
                continue
            print(f"  club={r.club_id} {posted_key} = {r.value}")
            if not dry_run:
                db.add(ClubSetting(club_id=r.club_id, key=posted_key, value=r.value))
            written += 1

        if dry_run:
            print(f"\nDRY RUN — would write {written}, leave {skipped} already set.")
            return

        db.commit()
        print(f"\nBackfilled {written} posted_week marker(s); {skipped} already set.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
