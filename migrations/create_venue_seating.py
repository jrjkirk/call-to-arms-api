"""Seating, schema step: create venue_seatings and venue_seats.

Two brand-new tables, no ALTERs — nothing existing changes shape. Until a
member of venue staff presses "Lay out the tables" on a date, neither has a
row, and every screen behaves exactly as it did before.

Order matters: venue_seats carries a foreign key to venue_seatings, so the
parent has to exist first.

    PYTHONPATH=. python migrations/create_venue_seating.py
    PYTHONPATH=. python migrations/create_venue_seating.py --verify-only

Safe to re-run: creation uses SQLModel's checkfirst.
"""
import sys

from sqlalchemy import inspect

from database import engine
from models import VenueSeat, VenueSeating

TABLES = [VenueSeating, VenueSeat]


def create_tables() -> None:
    for model in TABLES:
        model.metadata.create_all(engine, tables=[model.__table__], checkfirst=True)
        print(f"  {model.__tablename__} ready")


def verify() -> bool:
    names = set(inspect(engine).get_table_names())
    ok = True
    for model in TABLES:
        present = model.__tablename__ in names
        print(f"{model.__tablename__}: {'present' if present else 'MISSING'}")
        ok = ok and present
    return ok


def main() -> None:
    if "--verify-only" in sys.argv:
        sys.exit(0 if verify() else 1)
    create_tables()
    print()
    ok = verify()
    print("\nDone." if ok else "\nSomething is missing.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
