"""Bring venue_club_nights up to the shape the code expects, and drop the
orphaned venue_system_tables.

Why this exists: create_venue_management.py uses SQLModel's create_all with
checkfirst=True, which creates a MISSING table and does nothing at all to one
that already exists. When VenueClubNight grew the columns for nights this app
doesn't run (name/session_day/session_cadence/cadence_anchor/start_time/active)
and system_id became nullable, prod already had the table from the first run —
so the new columns never appeared, and the deployed code would have failed on
the first read of Venue Admin.

Also drops venue_system_tables, superseded by venue_night_tables when table
assignments moved from the game system onto the night (a venue-only night has
no system id to key on). Only dropped when empty, which it is everywhere:
venue management has never been switched on for a club.

    PYTHONPATH=. python migrations/fix_venue_club_nights_shape.py
    PYTHONPATH=. python migrations/fix_venue_club_nights_shape.py --verify-only

Safe to re-run: every step checks the current state first.
"""
import sys

from sqlalchemy import inspect, text

from database import engine

WANTED = {
    "name": "VARCHAR",
    "session_day": "VARCHAR",
    "session_cadence": "VARCHAR",
    "cadence_anchor": "DATE",
    "start_time": "VARCHAR",
    "active": "BOOLEAN NOT NULL DEFAULT TRUE",
}


def current_columns(table: str) -> set[str]:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    have = current_columns("venue_club_nights")
    if not have:
        print("venue_club_nights doesn't exist — run create_venue_management.py first.")
        sys.exit(1)

    missing = [c for c in WANTED if c not in have]
    print(f"venue_club_nights missing: {missing or 'nothing'}")

    if verify_only:
        insp = inspect(engine)
        stale = "venue_system_tables" in insp.get_table_names()
        print(f"venue_system_tables still present: {stale}")
        sys.exit(0 if not missing and not stale else 1)

    with engine.begin() as conn:
        for col in missing:
            conn.execute(text(
                f"ALTER TABLE venue_club_nights ADD COLUMN {col} {WANTED[col]}"
            ))
            print(f"  added {col}")

        # NOTE: every catalogue read below goes through `conn`, never through
        # inspect(engine). SQLAlchemy's inspector checks out its OWN connection,
        # and asking it about a table this transaction has just ALTERed means
        # waiting on the ACCESS EXCLUSIVE lock we are ourselves holding — which
        # is a self-deadlock that ends at the statement timeout. It did exactly
        # that on the first prod run, after the six ADD COLUMNs had landed.

        # system_id was NOT NULL when every night came from a game system. A
        # venue-only night has none, so the constraint has to go or Magic and
        # Bolt Action can never be saved.
        is_nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'venue_club_nights' AND column_name = 'system_id'"
        )).scalar()
        if is_nullable == "NO":
            conn.execute(text(
                "ALTER TABLE venue_club_nights ALTER COLUMN system_id DROP NOT NULL"
            ))
            print("  system_id is now nullable (venue-only nights have no system)")
        else:
            print("  system_id already nullable")

        stale = conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'venue_system_tables'"
        )).first()
        if stale:
            n = conn.execute(text("SELECT count(*) FROM venue_system_tables")).scalar()
            if n:
                # Refuse rather than guess. Nothing should be in here, and if
                # something is, someone's assignments are about to vanish.
                print(f"  !! venue_system_tables has {n} rows — NOT dropping it. "
                      f"Move them to venue_night_tables by hand first.")
            else:
                conn.execute(text("DROP TABLE venue_system_tables"))
                print("  dropped venue_system_tables (empty, superseded)")

    print("\nDone.")


if __name__ == "__main__":
    main()
