"""Venue management, schema step: create the venue tables.

Six brand-new tables, no ALTERs — the club IS the venue, so nothing existing
changes shape. Nothing is public until a super-admin opens Venue Admin, adds a
table and sets VenueConfig.enabled, so running this is inert on its own.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Same shape as
create_table_booking.py.

    PYTHONPATH=. python migrations/create_venue_management.py
    PYTHONPATH=. python migrations/create_venue_management.py --verify-only

Safe to re-run: creation uses SQLModel's checkfirst.
"""
import sys

from sqlalchemy import inspect

from database import engine
from models import (
    VenueBooking, VenueClubNight, VenueConfig, VenueNightTable, VenueStaff,
    VenueTable,
)

# Order matters: venue_bookings and venue_night_tables carry foreign keys to
# venue_tables, and venue_night_tables to venue_club_nights, so each has to
# exist before the rows that point at it.
TABLES = [
    VenueConfig, VenueTable, VenueBooking, VenueStaff,
    VenueClubNight, VenueNightTable,
]


def create_tables() -> None:
    for model in TABLES:
        model.metadata.create_all(engine, tables=[model.__table__], checkfirst=True)
        print(f"  {model.__tablename__} ready")


def verify() -> bool:
    names = set(inspect(engine).get_table_names())
    ok = True
    for model in TABLES:
        present = model.__tablename__ in names
        print(f"  {model.__tablename__}: {'present' if present else 'MISSING'}")
        ok = ok and present
    return ok


def main() -> None:
    if "--verify-only" in sys.argv:
        print("Verifying venue tables:")
        sys.exit(0 if verify() else 1)
    print("Creating venue tables:")
    create_tables()
    print("\nVerifying:")
    if not verify():
        sys.exit(1)
    print("\nDone. Nothing is public until a super-admin opens Venue Admin, "
          "adds a table and switches bookings on.")


if __name__ == "__main__":
    main()
