"""Add multi-day and schedule columns to tournaments.

The table already exists in production with real rows, so these are explicit
ALTERs rather than a create_all.

    PYTHONPATH=. python migrations/add_tournament_schedule.py
    PYTHONPATH=. python migrations/add_tournament_schedule.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

COLUMNS = [
    ("end_date", "DATE"),
    ("days", "INTEGER NOT NULL DEFAULT 1"),
    ("round_minutes", "INTEGER NOT NULL DEFAULT 150"),
    ("schedule", "JSON"),
]


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    ok = True
    with engine.begin() as conn:
        for col, ddl in COLUMNS:
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'tournaments' AND column_name = :c"
            ), {"c": col}).first()
            print(f"tournaments.{col}: {'present' if has else 'MISSING'}")
            ok = ok and bool(has)
            if not has and not verify_only:
                conn.execute(text(f"ALTER TABLE tournaments ADD COLUMN {col} {ddl}"))
                print(f"  added tournaments.{col}")
    if verify_only:
        sys.exit(0 if ok else 1)
    print("\nDone.")


if __name__ == "__main__":
    main()
