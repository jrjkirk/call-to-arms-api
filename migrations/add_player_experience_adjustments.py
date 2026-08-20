"""Per-(player, system) "games played elsewhere" adjustment (2026-08-20).

Backs the experience tiers on the signup form. The COUNT itself is derived
from pairings on demand and needs no storage or backfill — this table only
holds the manual addition a player makes when the club's tally misses games
they played before joining, or outside tracked events.

Additive and idempotent. Creates nothing that existing code reads, so it is
safe to run before the deploy — unlike a column added to an existing table,
this can't break the crons that select whole models from main.

    PYTHONPATH=. python migrations/add_player_experience_adjustments.py --create
    PYTHONPATH=. python migrations/add_player_experience_adjustments.py --verify

And against production:

    fly ssh console -C "sh -c 'cd /app && PYTHONPATH=. python migrations/add_player_experience_adjustments.py --create'"
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def create():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS player_experience_adjustments (
                id SERIAL PRIMARY KEY,
                club_id INTEGER NOT NULL REFERENCES clubs(id),
                player_id INTEGER NOT NULL REFERENCES players(id),
                system VARCHAR NOT NULL,
                extra_games INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        # One row per player per system per club, and it's also the exact
        # lookup adjustment_for() does.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_pea_club_player_system "
            "ON player_experience_adjustments (club_id, player_id, system)"
        ))
    print("Created player_experience_adjustments (or already present).")


def verify():
    with Session(engine) as db:
        n = db.exec(text("SELECT count(*) FROM player_experience_adjustments")).one()
        print("rows:", n[0] if isinstance(n, tuple) else n)
        print("OK")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args & {"--create", "--verify"}:
        print(__doc__)
        sys.exit(1)
    if "--create" in args:
        create()
    if "--verify" in args:
        verify()
