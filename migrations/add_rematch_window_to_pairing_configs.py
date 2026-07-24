"""Add nullable recent_weeks / extended_weeks to pairing_configs — a per-club
override of the rematch lookback windows that otherwise come from the platform
SystemConfig. NULL means "use the SystemConfig default", so existing rows keep
today's behaviour unchanged.

Run (staging then prod):
    PYTHONPATH=. python migrations/add_rematch_window_to_pairing_configs.py
    PYTHONPATH=. python migrations/add_rematch_window_to_pairing_configs.py --verify-only

Prod (via Fly):
    fly ssh console -C "sh -c 'cd /app && PYTHONPATH=. python migrations/add_rematch_window_to_pairing_configs.py'"

Safe to re-run: ADD COLUMN IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def migrate():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE pairing_configs ADD COLUMN IF NOT EXISTS recent_weeks INTEGER"))
        conn.execute(text("ALTER TABLE pairing_configs ADD COLUMN IF NOT EXISTS extended_weeks INTEGER"))
    print("Added pairing_configs.recent_weeks / extended_weeks (nullable).")


def verify() -> None:
    with Session(engine) as session:
        cols = {r[0] for r in session.exec(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='pairing_configs'"
        )).all()}
        missing = {"recent_weeks", "extended_weeks"} - cols
        if missing:
            print(f"VERIFICATION FAILED: missing {missing}")
            sys.exit(1)
    print("Verification passed: pairing_configs.recent_weeks / extended_weeks present.")


def main():
    if "--verify-only" not in sys.argv:
        migrate()
    verify()


if __name__ == "__main__":
    main()
