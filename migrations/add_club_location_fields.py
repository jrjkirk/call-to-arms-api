"""Club landing page follow-up: adds address/latitude/longitude to `clubs`.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Same shape as
create_club_page_fields.py — all three columns nullable, no contract step.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/add_club_location_fields.py
    PYTHONPATH=. python migrations/add_club_location_fields.py --verify-only

Safe to re-run: ADD COLUMN uses IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_columns():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE clubs ADD COLUMN IF NOT EXISTS address TEXT"))
        conn.execute(text("ALTER TABLE clubs ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE clubs ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION"))
    print("Added address/latitude/longitude columns to clubs (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        cols = {row[0] for row in session.exec(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'clubs'"
        )).all()}
        for col in ("address", "latitude", "longitude"):
            if col not in cols:
                problems.append(f"clubs missing column {col}")
    return problems


def main():
    if "--verify-only" not in sys.argv:
        add_columns()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: clubs.address/latitude/longitude present.")


if __name__ == "__main__":
    main()
