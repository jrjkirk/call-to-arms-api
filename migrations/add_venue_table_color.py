"""Add `color` to venue_tables.

venue_tables is already deployed, so create_all(checkfirst=True) will not touch
it — explicit ALTER, as with every other venue column added after the fact.

Defaults to 'slate', which is what every existing table is drawn as today.

    PYTHONPATH=. python migrations/add_venue_table_color.py
    PYTHONPATH=. python migrations/add_venue_table_color.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        has = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'venue_tables' AND column_name = 'color'"
        )).first()
        print(f"venue_tables.color: {'present' if has else 'MISSING'}")
        if verify_only:
            sys.exit(0 if has else 1)
        if not has:
            conn.execute(text(
                "ALTER TABLE venue_tables ADD COLUMN color VARCHAR NOT NULL DEFAULT 'slate'"
            ))
            print("  added venue_tables.color")
    print("\nDone.")


if __name__ == "__main__":
    main()
