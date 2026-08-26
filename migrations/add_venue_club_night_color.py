"""Add `color` to venue_club_nights.

Already-deployed table, so create_all(checkfirst=True) won't touch it —
explicit ALTER, as with every other venue column added after the fact.

Defaults to 'amber', which is the gold every held table already draws as, so
nothing changes appearance until a venue picks something else.

    PYTHONPATH=. python migrations/add_venue_club_night_color.py
    PYTHONPATH=. python migrations/add_venue_club_night_color.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        has = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'venue_club_nights' AND column_name = 'color'"
        )).first()
        print(f"venue_club_nights.color: {'present' if has else 'MISSING'}")
        if verify_only:
            sys.exit(0 if has else 1)
        if not has:
            conn.execute(text(
                "ALTER TABLE venue_club_nights ADD COLUMN color VARCHAR NOT NULL DEFAULT 'amber'"
            ))
            print("  added venue_club_nights.color")
    print("\nDone.")


if __name__ == "__main__":
    main()
