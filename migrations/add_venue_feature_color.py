"""Add `color` to venue_features.

venue_features is already deployed, so create_all(checkfirst=True) will not
touch it — explicit ALTER, as with every other venue column added after the
fact. Defaults to 'grey', which is what fixtures already draw as.

    PYTHONPATH=. python migrations/add_venue_feature_color.py
    PYTHONPATH=. python migrations/add_venue_feature_color.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        has = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'venue_features' AND column_name = 'color'"
        )).first()
        print(f"venue_features.color: {'present' if has else 'MISSING'}")
        if verify_only:
            sys.exit(0 if has else 1)
        if not has:
            conn.execute(text(
                "ALTER TABLE venue_features ADD COLUMN color VARCHAR NOT NULL DEFAULT 'grey'"
            ))
            print("  added venue_features.color")
    print("\nDone.")


if __name__ == "__main__":
    main()
