"""Create the four tournament tables.

    PYTHONPATH=. python migrations/create_tournaments.py
    PYTHONPATH=. python migrations/create_tournaments.py --verify-only

Kept deliberately compact: `fly ssh console -C` hangs on a base64 payload much
over ~1.5k chars, and this is how it gets run on production.
"""
import sys

from sqlalchemy import text

from database import engine
from models import Tournament, TournamentEntry, TournamentGame, TournamentRound

TABLES = ("tournaments", "tournament_entries", "tournament_rounds", "tournament_games")


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    if not verify_only:
        # All four are new, so create_all is safe and correct here — unlike the
        # venue migrations, which had to ALTER tables that already existed.
        Tournament.metadata.create_all(engine, tables=[
            Tournament.__table__, TournamentEntry.__table__,
            TournamentRound.__table__, TournamentGame.__table__,
        ], checkfirst=True)

    ok = True
    with engine.begin() as conn:
        for t in TABLES:
            found = conn.execute(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
            ), {"t": t}).first()
            print(f"{t}: {'present' if found else 'MISSING'}")
            ok = ok and bool(found)

    if verify_only:
        sys.exit(0 if ok else 1)
    print("\nDone.")


if __name__ == "__main__":
    main()
