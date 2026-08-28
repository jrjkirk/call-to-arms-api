"""Let people book a table without an account.

Three changes, all on already-deployed tables, so create_all(checkfirst=True)
won't touch them -- explicit ALTERs, as with every other venue column added
after the fact:

  venue_bookings.user_id      NOT NULL -> NULL   (a guest has no account)
  venue_bookings.manage_token new column         (cancel link in their email)
  venue_configs               three policy columns

Defaults keep every existing venue exactly as it was on the member path:
guest_bookings is on, but guests land in "request" so a human sees each one,
and require_phone starts true because a venue chasing a no-show wants a number
it can ring.

    PYTHONPATH=. python migrations/add_guest_bookings.py
    PYTHONPATH=. python migrations/add_guest_bookings.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

COLUMNS = [
    ("venue_bookings", "manage_token", "VARCHAR"),
    ("venue_configs", "guest_bookings", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("venue_configs", "guest_confirm_mode", "VARCHAR NOT NULL DEFAULT 'request'"),
    ("venue_configs", "require_phone", "BOOLEAN NOT NULL DEFAULT TRUE"),
]


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    ok = True
    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            has = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ), {"t": table, "c": column}).first()
            print(f"{table}.{column}: {'present' if has else 'MISSING'}")
            ok = ok and bool(has)
            if not has and not verify_only:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                print(f"  added {table}.{column}")

        # The index is what makes the cancel link a single-row lookup rather
        # than a scan of every booking the venue has ever taken.
        if not verify_only:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_venue_bookings_manage_token "
                "ON venue_bookings (manage_token)"
            ))

        nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'venue_bookings' AND column_name = 'user_id'"
        )).scalar()
        print(f"venue_bookings.user_id nullable: {nullable}")
        ok = ok and nullable == "YES"
        if nullable == "NO" and not verify_only:
            conn.execute(text(
                "ALTER TABLE venue_bookings ALTER COLUMN user_id DROP NOT NULL"
            ))
            print("  dropped NOT NULL on venue_bookings.user_id")

    if verify_only:
        sys.exit(0 if ok else 1)
    print("\nDone.")


if __name__ == "__main__":
    main()
