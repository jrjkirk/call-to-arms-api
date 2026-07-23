"""Pairing weighting config, schema step (expand only — new table, no data change).

Creates `pairing_configs` (one row per club/system, admin-set weights for the
soft matchmaking factors — see pairings_engine._pair_dist / models.PairingConfig).
No backfill needed: rows are created lazily on first save, and reads fall back
to an unsaved-defaults instance until then (same convention as league_configs).

Run (staging then prod):
    PYTHONPATH=. python migrations/create_pairing_configs.py
    PYTHONPATH=. python migrations/create_pairing_configs.py --verify-only

Safe to re-run: CREATE via checkfirst.
"""
import sys

from sqlmodel import Session
from sqlalchemy import text

from database import engine
from models import PairingConfig


def create_tables():
    PairingConfig.metadata.create_all(engine, tables=[PairingConfig.__table__], checkfirst=True)
    print("Created pairing_configs (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        if not session.exec(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name='pairing_configs'"
        )).first():
            problems.append("pairing_configs table does not exist")

    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed: pairing_configs table present.")
    return problems


def main():
    if "--verify-only" not in sys.argv:
        create_tables()
    verify()


if __name__ == "__main__":
    main()
