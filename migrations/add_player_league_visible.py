"""Add players.league_visible — the second half of splitting the overloaded
`active` flag into two independent switches.

`active` used to mean everything at once: on the roster, allowed to sign up,
listed in the league, AND — fatally — "this account has a profile". That last
one is what broke: archiving a player made active_player_id_for return None,
so the app offered them a fresh profile and they got a second, empty Player
row (see the note there). That half of the fix is code-only.

This column carries the part of `active` that clubs actually wanted a switch
for: leave someone out of league standings without taking them off the roster.

Defaults TRUE so every existing player keeps appearing exactly as they do now.
NOT NULL with a default is a metadata-only change on Postgres 11+ — no table
rewrite, no lock worth worrying about at our size.

    PYTHONPATH=. python migrations/add_player_league_visible.py
"""
from sqlalchemy import text

from database import engine


def main() -> None:
    with engine.begin() as conn:
        existing = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'players' AND column_name = 'league_visible'"
        )).first()
        if existing:
            print("players.league_visible already exists — nothing to do.")
            return

        conn.execute(text(
            "ALTER TABLE players "
            "ADD COLUMN league_visible BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        print("Added players.league_visible (NOT NULL, default TRUE).")

        n = conn.execute(text("SELECT count(*) FROM players")).scalar()
        print(f"{n} existing players are league-visible by default.")


if __name__ == "__main__":
    main()
