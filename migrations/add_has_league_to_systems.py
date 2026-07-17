"""Phase B (league-as-capability): add has_league to the systems catalogue.

Generalizes league eligibility from a hardcoded `system == "The Old World"`
name check into per-system catalogue data, so a future league-eligible
system needs no code change.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Unlike the club_id
expand/backfill/contract scripts, has_league carries a NOT NULL DEFAULT
FALSE, so every existing row gets a valid value the instant the column is
added — there is no nullable window and no separate --contract step. The
backfill then flips The Old World (the only league system today) to True.

Run manually, in order:

    PYTHONPATH=. python migrations/add_has_league_to_systems.py --add-column
    PYTHONPATH=. python migrations/add_has_league_to_systems.py --backfill
    PYTHONPATH=. python migrations/add_has_league_to_systems.py --verify

Safe to re-run: ADD COLUMN uses IF NOT EXISTS; backfill is an idempotent
set-by-legacy_system_name.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import SystemConfig

# The only league-eligible system today. Backfilled to has_league=True; all
# other catalogue rows keep the DEFAULT FALSE.
LEAGUE_SYSTEMS = ["The Old World"]


def add_column():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE systems ADD COLUMN IF NOT EXISTS has_league "
            "BOOLEAN NOT NULL DEFAULT FALSE"
        ))
    print("Added has_league column to systems (or already present).")


def backfill():
    with Session(engine) as session:
        rows = session.exec(select(SystemConfig)).all()
        flipped = 0
        for r in rows:
            want = r.legacy_system_name in LEAGUE_SYSTEMS
            if r.has_league != want:
                r.has_league = want
                session.add(r)
                flipped += 1
        session.commit()
        print(f"systems: {len(rows)} row(s); set has_league on {flipped} row(s).")
        for r in rows:
            print(f"  - {r.legacy_system_name!r}: has_league={r.has_league}")


def verify():
    problems = []
    with Session(engine) as session:
        rows = session.exec(select(SystemConfig)).all()
        for r in rows:
            want = r.legacy_system_name in LEAGUE_SYSTEMS
            if r.has_league != want:
                problems.append(
                    f"{r.legacy_system_name!r}: has_league={r.has_league}, expected {want}"
                )
    print(f"systems: {len(rows)} total row(s), league systems = {LEAGUE_SYSTEMS}")
    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed.")


def main():
    if "--add-column" in sys.argv:
        add_column()
    if "--backfill" in sys.argv:
        backfill()
    if "--verify" in sys.argv:
        verify()
    if len(sys.argv) == 1:
        print(__doc__)


if __name__ == "__main__":
    main()
