"""Add the approval columns to venue_events.

venue_events was created a deploy ago, so create_all(checkfirst=True) will not
touch it — it only ever creates a MISSING table. Same gap that left
venue_club_nights six columns short; see fix_venue_club_nights_shape.py.

Existing rows are back-filled to 'approved'. They were created before events
needed a yes, and retroactively suspending a tournament somebody has already
been told is happening would be a worse answer than grandfathering it.

Catalogue reads go through the transaction's own connection, never
inspect(engine) — that checks out a second connection and self-deadlocks on the
lock this transaction holds.

    PYTHONPATH=. python migrations/add_venue_event_approval.py
    PYTHONPATH=. python migrations/add_venue_event_approval.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

WANTED = {
    "status": "VARCHAR NOT NULL DEFAULT 'approved'",
    "rejection_reason": "VARCHAR",
    "approved_by_user_id": "INTEGER",
    "approved_at": "TIMESTAMP",
}


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        if not conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'venue_events'"
        )).first():
            print("venue_events doesn't exist — run add_venue_events.py first.")
            sys.exit(1)

        have = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venue_events'"
        ))}
        missing = [c for c in WANTED if c not in have]
        print(f"venue_events missing: {missing or 'nothing'}")
        if verify_only:
            sys.exit(0 if not missing else 1)

        for col in missing:
            conn.execute(text(f"ALTER TABLE venue_events ADD COLUMN {col} {WANTED[col]}"))
            print(f"  added {col}")

        if "status" in missing:
            # The DEFAULT above already filled existing rows; new rows come from
            # the model, which defaults to 'pending'. Drop the server default so
            # the two can't disagree about what a fresh event starts as.
            conn.execute(text(
                "ALTER TABLE venue_events ALTER COLUMN status DROP DEFAULT"
            ))
            print("  existing events grandfathered to approved; server default dropped")

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_venue_events_status ON venue_events (status)"
        ))
    print("\nDone.")


if __name__ == "__main__":
    main()
