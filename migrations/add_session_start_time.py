"""Club calendar follow-up: adds session_start_time to `club_systems`, so the
Club page calendar's auto-derived session entries can show a time (e.g.
"The Horus Heresy session 18:00") instead of just an all-day entry.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Nullable column, no
contract step needed.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/add_session_start_time.py
    PYTHONPATH=. python migrations/add_session_start_time.py --verify-only

Safe to re-run: ADD COLUMN uses IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_column():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS session_start_time TEXT"
        ))
    print("Added session_start_time column to club_systems (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        cols = {row[0] for row in session.exec(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'club_systems'"
        )).all()}
        if "session_start_time" not in cols:
            problems.append("club_systems missing column session_start_time")
    return problems


def main():
    if "--verify-only" not in sys.argv:
        add_column()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: club_systems.session_start_time present.")


if __name__ == "__main__":
    main()
