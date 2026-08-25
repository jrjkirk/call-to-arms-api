"""Add `shape` to venue_tables and venue_features.

Both tables are already deployed, so create_all(checkfirst=True) will not touch
them — it only ever creates a MISSING table. Explicit ALTERs, as with every
other venue column added after the fact.

Defaults to 'rect', which is what every existing row already is.

    PYTHONPATH=. python migrations/add_venue_shapes.py
    PYTHONPATH=. python migrations/add_venue_shapes.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        missing = []
        for table in ("venue_tables", "venue_features"):
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                f"WHERE table_name = '{table}' AND column_name = 'shape'"
            )).first()
            if not has:
                missing.append(table)
        print(f"missing shape on: {missing or 'nothing'}")
        if verify_only:
            sys.exit(0 if not missing else 1)
        for table in missing:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN shape VARCHAR NOT NULL DEFAULT 'rect'"
            ))
            print(f"  added {table}.shape")
    print("\nDone.")


if __name__ == "__main__":
    main()
