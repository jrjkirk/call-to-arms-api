"""Create venue_rooms and venue_features, and give venue_tables its geometry.

TWO KINDS OF STEP, because create_all(checkfirst=True) only ever CREATES a
missing table — it will not add a column to one that already exists.
venue_tables is already deployed, so its six new columns need explicit ALTERs.
That gap is what left venue_club_nights short six columns; see
fix_venue_club_nights_shape.py.

Catalogue reads go through the transaction's own connection rather than
inspect(engine), which checks out a SECOND connection and self-deadlocks on the
ACCESS EXCLUSIVE lock this transaction holds.

Existing tables get NULL room_id/pos and the standard 6x4 footprint. They are
placed the first time someone opens the plan (venue.ensure_default_room →
autoplace), not here: laying out a room is a judgement about a real space, and
doing it in a migration would guess at one nobody has described yet.

    PYTHONPATH=. python migrations/add_venue_floorplan.py
    PYTHONPATH=. python migrations/add_venue_floorplan.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine
from models import VenueFeature, VenueRoom

COLUMNS = {
    "room_id": "INTEGER",
    "pos_x": "DOUBLE PRECISION",
    "pos_y": "DOUBLE PRECISION",
    "width_ft": "DOUBLE PRECISION NOT NULL DEFAULT 6.0",
    "depth_ft": "DOUBLE PRECISION NOT NULL DEFAULT 4.0",
    "rotation": "DOUBLE PRECISION NOT NULL DEFAULT 0.0",
}


def main() -> None:
    verify_only = "--verify-only" in sys.argv

    with engine.begin() as conn:
        have = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venue_tables'"
        ))}
        tables = {r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('venue_rooms', 'venue_features')"
        ))}
        missing_cols = [c for c in COLUMNS if c not in have]
        missing_tables = [t for t in ("venue_rooms", "venue_features") if t not in tables]

        print(f"venue_tables missing: {missing_cols or 'nothing'}")
        print(f"tables missing: {missing_tables or 'nothing'}")
        if verify_only:
            sys.exit(0 if not missing_cols and not missing_tables else 1)

        for col in missing_cols:
            conn.execute(text(f"ALTER TABLE venue_tables ADD COLUMN {col} {COLUMNS[col]}"))
            print(f"  added venue_tables.{col}")
        if missing_cols:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_venue_tables_room_id "
                "ON venue_tables (room_id)"
            ))

    # Outside the transaction above: create_all opens its own connection, and
    # doing that while holding a lock on venue_tables is the deadlock the
    # docstring warns about.
    for model in (VenueRoom, VenueFeature):
        model.metadata.create_all(engine, tables=[model.__table__], checkfirst=True)
        print(f"  {model.__tablename__} ready")

    # Last, so venue_rooms exists to point at.
    with engine.begin() as conn:
        if not conn.execute(text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = 'venue_tables' AND constraint_type = 'FOREIGN KEY' "
            "AND constraint_name = 'venue_tables_room_id_fkey'"
        )).first():
            conn.execute(text(
                "ALTER TABLE venue_tables ADD CONSTRAINT venue_tables_room_id_fkey "
                "FOREIGN KEY (room_id) REFERENCES venue_rooms (id)"
            ))
            print("  linked venue_tables.room_id -> venue_rooms.id")

    print("\nDone. Rooms are laid out on first open of the plan, not here.")


if __name__ == "__main__":
    main()
