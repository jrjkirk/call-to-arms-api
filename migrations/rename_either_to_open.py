"""Rename the "Either" game vibe to "Open".

It reads better standing on its own beside a checkbox — "Either" answers a
question the signup form's label doesn't ask.

The value is stored, not just displayed, so this touches four places:

    systems.vibe_options        the platform catalogue (JSON array)
    systems.default_vibe
    club_systems.vibe_options   a club's override, if it set one
    club_systems.default_vibe
    signups.vibe                ~200 historical rows at the time of writing

The code normalises "Either" -> "Open" on every read regardless
(signups.OLD_VIBE_ALIASES), so this migration is a tidy-up rather than a
prerequisite: it is safe to run before OR after the deploy, and safe to run
twice. That was deliberate — a rename of a value living in thousands of rows
should not depend on one UPDATE catching every one of them.

    PYTHONPATH=. python migrations/rename_either_to_open.py
    PYTHONPATH=. python migrations/rename_either_to_open.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

# JSON arrays are rewritten by text substitution on the serialised value:
# the column is JSON, the members are plain strings, and "Either" appears
# nowhere else in them.
STEPS = [
    ("systems.vibe_options",
     "UPDATE systems SET vibe_options = REPLACE(vibe_options::text, '\"Either\"', '\"Open\"')::json "
     "WHERE vibe_options::text LIKE '%\"Either\"%'"),
    ("systems.default_vibe",
     "UPDATE systems SET default_vibe = 'Open' WHERE default_vibe = 'Either'"),
    ("club_systems.vibe_options",
     "UPDATE club_systems SET vibe_options = REPLACE(vibe_options::text, '\"Either\"', '\"Open\"')::json "
     "WHERE vibe_options::text LIKE '%\"Either\"%'"),
    ("club_systems.default_vibe",
     "UPDATE club_systems SET default_vibe = 'Open' WHERE default_vibe = 'Either'"),
    ("signups.vibe",
     "UPDATE signups SET vibe = 'Open' WHERE vibe = 'Either'"),
]

COUNTS = [
    ("systems.vibe_options", "SELECT count(*) FROM systems WHERE vibe_options::text LIKE '%\"Either\"%'"),
    ("systems.default_vibe", "SELECT count(*) FROM systems WHERE default_vibe = 'Either'"),
    ("club_systems.vibe_options", "SELECT count(*) FROM club_systems WHERE vibe_options::text LIKE '%\"Either\"%'"),
    ("club_systems.default_vibe", "SELECT count(*) FROM club_systems WHERE default_vibe = 'Either'"),
    ("signups.vibe", "SELECT count(*) FROM signups WHERE vibe = 'Either'"),
]


def report(conn, header):
    print(header)
    total = 0
    for label, q in COUNTS:
        n = conn.execute(text(q)).scalar() or 0
        total += n
        print(f"  {label:<26} {n}")
    return total


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        remaining = report(conn, "Rows still saying 'Either':")
        if verify_only:
            print("\nClean." if remaining == 0 else f"\n{remaining} row(s) left.")
            sys.exit(0 if remaining == 0 else 1)
        if remaining == 0:
            print("\nNothing to do.")
            return
        print()
        for label, q in STEPS:
            n = conn.execute(text(q)).rowcount
            print(f"  {label:<26} updated {n}")

    with engine.begin() as conn:
        left = report(conn, "\nAfter:")
    print("\nDone." if left == 0 else f"\n{left} row(s) still left — investigate.")
    sys.exit(0 if left == 0 else 1)


if __name__ == "__main__":
    main()
