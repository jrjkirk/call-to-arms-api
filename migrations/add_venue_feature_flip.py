"""Add `flip_h` / `flip_v` to venue_features.

venue_features is already deployed, so create_all(checkfirst=True) will not
touch it — explicit ALTER, as with every other venue column added after the
fact. Both default FALSE, which is how every existing fixture already draws.

    PYTHONPATH=. python migrations/add_venue_feature_flip.py
    PYTHONPATH=. python migrations/add_venue_feature_flip.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

COLUMNS = ("flip_h", "flip_v")


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        have = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'venue_features'"
        ))}
        missing = [c for c in COLUMNS if c not in have]
        print(f"venue_features missing: {missing or 'nothing'}")
        if verify_only:
            sys.exit(0 if not missing else 1)
        for col in missing:
            conn.execute(text(
                f"ALTER TABLE venue_features ADD COLUMN {col} BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            print(f"  added venue_features.{col}")
    print("\nDone.")


if __name__ == "__main__":
    main()
