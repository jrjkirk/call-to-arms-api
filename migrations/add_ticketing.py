"""Ticketing columns: Stripe Connect on clubs, payment trail on entries.

The tables already exist in production with rows, so these are explicit ALTERs.
Already applied to production 2026-08-31.

    PYTHONPATH=. python migrations/add_ticketing.py
    PYTHONPATH=. python migrations/add_ticketing.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

COLUMNS = [
    ("clubs", "stripe_account_id", "VARCHAR"),
    ("clubs", "stripe_charges_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("tournaments", "ticket_hold_hours", "INTEGER NOT NULL DEFAULT 72"),
    ("tournament_entries", "stripe_session_id", "VARCHAR"),
    ("tournament_entries", "stripe_payment_intent", "VARCHAR"),
    ("tournament_entries", "amount_paid_pence", "INTEGER"),
    ("tournament_entries", "paid_at", "TIMESTAMP"),
    ("tournament_entries", "hold_expires_at", "TIMESTAMP"),
    ("tournament_entries", "waitlisted_at", "TIMESTAMP"),
]

INDEXES = [
    ("ix_te_hold", "tournament_entries (hold_expires_at)"),
    ("ix_te_intent", "tournament_entries (stripe_payment_intent)"),
]


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    ok = True
    with engine.begin() as conn:
        for table, col, ddl in COLUMNS:
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ), {"t": table, "c": col}).first()
            print(f"{table}.{col}: {'present' if has else 'MISSING'}")
            ok = ok and bool(has)
            if not has and not verify_only:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                print(f"  added {table}.{col}")
        if not verify_only:
            for name, spec in INDEXES:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}"))
    if verify_only:
        sys.exit(0 if ok else 1)
    print("\nDone.")


if __name__ == "__main__":
    main()
