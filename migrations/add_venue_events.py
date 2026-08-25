"""Create venue_events and add venue_bookings.event_id.

TWO STEPS, because create_all(checkfirst=True) only ever CREATES a missing
table — it will not add a column to one that already exists. venue_bookings is
already out there, so its new event_id column needs an explicit ALTER. That
exact gap is what left prod's venue_club_nights missing six columns; see
fix_venue_club_nights_shape.py.

Every catalogue read goes through the transaction's own connection rather than
inspect(engine), which checks out a SECOND connection and would block on the
ACCESS EXCLUSIVE lock this transaction holds — a self-deadlock that ends at the
statement timeout, as it did on the first run of that other migration.

    PYTHONPATH=. python migrations/add_venue_events.py
    PYTHONPATH=. python migrations/add_venue_events.py --verify-only

Safe to re-run: both steps check the current state first.
"""
import sys

from sqlalchemy import text

from database import engine
from models import VenueEvent


def main() -> None:
    verify_only = "--verify-only" in sys.argv

    with engine.begin() as conn:
        has_table = conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'venue_events'"
        )).first() is not None
        has_column = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'venue_bookings' AND column_name = 'event_id'"
        )).first() is not None

        print(f"venue_events table: {'present' if has_table else 'MISSING'}")
        print(f"venue_bookings.event_id: {'present' if has_column else 'MISSING'}")

        if verify_only:
            sys.exit(0 if has_table and has_column else 1)

        if not has_column:
            # Nullable with no default: an ordinary booking has no event, and
            # that is the overwhelming majority of rows.
            conn.execute(text(
                "ALTER TABLE venue_bookings ADD COLUMN event_id INTEGER"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_venue_bookings_event_id "
                "ON venue_bookings (event_id)"
            ))
            print("  added venue_bookings.event_id")

    # Outside the transaction above: create_all opens its own connection, and
    # doing that while holding a lock on venue_bookings is the deadlock this
    # module's docstring warns about.
    if not has_table:
        VenueEvent.metadata.create_all(
            engine, tables=[VenueEvent.__table__], checkfirst=True
        )
        print("  created venue_events")

    # The foreign key is added last: venue_events has to exist first, and
    # venue_bookings must already have the column to point with.
    with engine.begin() as conn:
        fk = conn.execute(text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = 'venue_bookings' AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = 'venue_bookings_event_id_fkey'"
        )).first()
        if not fk:
            conn.execute(text(
                "ALTER TABLE venue_bookings ADD CONSTRAINT venue_bookings_event_id_fkey "
                "FOREIGN KEY (event_id) REFERENCES venue_events (id)"
            ))
            print("  linked venue_bookings.event_id -> venue_events.id")

    print("\nDone.")


if __name__ == "__main__":
    main()
