"""Add SystemConfig.uses_standby (systems table) — the standby signup option,
previously hardcoded to The Old World, becomes a per-system catalogue
capability. Backfills uses_standby=true for The Old World only, preserving
today's behaviour; every other system defaults false.

Run (staging then prod):
    PYTHONPATH=. python migrations/add_uses_standby_to_systems.py
    PYTHONPATH=. python migrations/add_uses_standby_to_systems.py --verify-only

Prod (via Fly, whose DATABASE_URL is the real prod DB):
    fly ssh console -C "sh -c 'cd /app && PYTHONPATH=. python migrations/add_uses_standby_to_systems.py'"

Safe to re-run: ADD COLUMN IF NOT EXISTS; backfill is idempotent.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def migrate():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE systems ADD COLUMN IF NOT EXISTS uses_standby BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "UPDATE systems SET uses_standby = TRUE WHERE legacy_system_name = 'The Old World'"
        ))
    print("Added systems.uses_standby (default false); backfilled The Old World = true.")


def verify() -> None:
    with Session(engine) as session:
        cols = {r[0] for r in session.exec(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='systems'"
        )).all()}
        if "uses_standby" not in cols:
            print("VERIFICATION FAILED: systems.uses_standby missing")
            sys.exit(1)
        rows = session.exec(text(
            "SELECT legacy_system_name, uses_standby FROM systems ORDER BY legacy_system_name"
        )).all()
    print("Verification passed. uses_standby by system:")
    for name, val in rows:
        print(f"  {name}: {val}")


def main():
    if "--verify-only" not in sys.argv:
        migrate()
    verify()


if __name__ == "__main__":
    main()
