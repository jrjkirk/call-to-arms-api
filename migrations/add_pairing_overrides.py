"""Pairing-level overrides (2026-09-16): pairings.eta / points / a_vibe / b_vibe.

Why: the admin pairings grid used to write its displayed values back onto both
Signup rows, so changing a game's time rewrote what each player had signed up
with (reported by Nick, 16/09). A signup is what the player asked for; the
pairing now holds what the admin agreed. NULL means "derive from the signups",
which is how every existing row behaves, so this changes nothing on its own.

Columns only, no data touched. RUN BEFORE DEPLOYING: the new code reads and
writes them. Old code ignores them, so running it early is harmless.

    PYTHONPATH=. python migrations/add_pairing_overrides.py                # dry run
    PYTHONPATH=. python migrations/add_pairing_overrides.py --apply
    PYTHONPATH=. python migrations/add_pairing_overrides.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

STATEMENTS = [
    "ALTER TABLE pairings ADD COLUMN IF NOT EXISTS eta VARCHAR",
    "ALTER TABLE pairings ADD COLUMN IF NOT EXISTS points INTEGER",
    "ALTER TABLE pairings ADD COLUMN IF NOT EXISTS a_vibe VARCHAR",
    "ALTER TABLE pairings ADD COLUMN IF NOT EXISTS b_vibe VARCHAR",
]
EXPECTED = {"eta", "points", "a_vibe", "b_vibe"}


def verify(conn) -> list[str]:
    have = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'pairings'"
    )).all()}
    if not have:
        return ["pairings table not found"]
    return [f"pairings.{c} missing" for c in sorted(EXPECTED - have)]


def main() -> None:
    if "--verify-only" in sys.argv:
        with engine.connect() as conn:
            problems = verify(conn)
    elif "--apply" in sys.argv:
        with engine.begin() as conn:
            for sql in STATEMENTS:
                conn.execute(text(sql))
            problems = verify(conn)
            if problems:
                raise RuntimeError(f"verification failed, rolling back: {problems}")
        print("APPLIED.")
    else:
        with engine.connect() as conn:
            problems = verify(conn)
        if problems:
            print("DRY RUN, nothing written. --apply would run:")
            for sql in STATEMENTS:
                print("  " + sql)
            print("  (outstanding: " + "; ".join(problems) + ")")
        else:
            print("DRY RUN, nothing written. All four columns already exist.")
        return

    if problems:
        print(f"VERIFICATION FAILED: {problems}")
        sys.exit(1)
    print("Verification passed.")


if __name__ == "__main__":
    main()
