"""Multi-club network model (2026-07-25): expand + backfill for the three
additive columns that let one Discord account play at any number of clubs.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). All three tables already
exist with real rows, so SQLModel's create_all() won't alter them; this runs
the ALTER TABLEs by hand, then backfills.

Columns added (all NULLABLE by design — no contract/SET NOT NULL step):
  - players.user_id      -> the Discord account that owns this player row.
                            NULL = an unclaimed roster entry. Backfilled by
                            inverting the existing users.player_id link.
  - users.home_club_id   -> soft default club. Backfilled from users.club_id.
  - clubs.region         -> one of models.UK_REGIONS. No backfill except a
                            convenience default for Manchester (North West);
                            every other club's region is set later via admin.

Run manually, in order:

    PYTHONPATH=. python migrations/add_network_columns.py --add-columns
    PYTHONPATH=. python migrations/add_network_columns.py --backfill
    PYTHONPATH=. python migrations/add_network_columns.py --verify

Safe to re-run: ALTER TABLE uses IF NOT EXISTS; backfill only touches rows
that are still NULL / not yet linked.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import Club, Player, User


def add_columns():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_players_user_id ON players(user_id)"
        ))
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS home_club_id INTEGER REFERENCES clubs(id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_users_home_club_id ON users(home_club_id)"
        ))
        conn.execute(text(
            "ALTER TABLE clubs ADD COLUMN IF NOT EXISTS region VARCHAR"
        ))
    print("Added players.user_id, users.home_club_id, clubs.region (or already present).")


def backfill():
    with Session(engine) as session:
        # 1. players.user_id <- invert users.player_id
        users_with_player = session.exec(
            select(User).where(User.player_id.is_not(None))
        ).all()
        linked = 0
        skipped = 0
        for u in users_with_player:
            player = session.get(Player, u.player_id)
            if player is None:
                # dangling User.player_id (player deleted) — nothing to link
                skipped += 1
                continue
            if player.user_id is None:
                player.user_id = u.id
                session.add(player)
                linked += 1
        print(f"players.user_id: linked {linked} player row(s) from users.player_id "
              f"({skipped} dangling user link(s) skipped).")

        # 2. users.home_club_id <- users.club_id
        users_no_home = session.exec(
            select(User).where(User.home_club_id.is_(None), User.club_id.is_not(None))
        ).all()
        for u in users_no_home:
            u.home_club_id = u.club_id
            session.add(u)
        print(f"users.home_club_id: backfilled {len(users_no_home)} row(s) from club_id.")

        # 3. clubs.region convenience default for Manchester only
        manchester = session.exec(select(Club).where(Club.slug == "manchester")).first()
        if manchester is not None and manchester.region is None:
            manchester.region = "North West"
            session.add(manchester)
            print("clubs.region: set Manchester -> 'North West'.")
        else:
            print("clubs.region: Manchester already set (or missing) — no change.")

        session.commit()


def verify():
    with Session(engine) as session:
        # Every user with a player_id should now have that player back-linked.
        users_with_player = session.exec(
            select(User).where(User.player_id.is_not(None))
        ).all()
        problems = []
        for u in users_with_player:
            player = session.get(Player, u.player_id)
            if player is None:
                continue  # dangling, legitimately unlinkable
            if player.user_id != u.id:
                problems.append(
                    f"user {u.id} ({u.discord_name}) -> player {u.player_id}, "
                    f"but player.user_id={player.user_id}"
                )

        # Every user with a club should have a home_club_id.
        missing_home = session.exec(
            select(User).where(User.home_club_id.is_(None), User.club_id.is_not(None))
        ).all()
        for u in missing_home:
            problems.append(f"user {u.id} ({u.discord_name}) has club_id but no home_club_id")

        total_users = len(session.exec(select(User)).all())
        total_players = len(session.exec(select(Player)).all())
        claimed_players = len(session.exec(
            select(Player).where(Player.user_id.is_not(None))
        ).all())
        # A user owning multiple players (one per club) is now VALID by design,
        # so there is deliberately no "one player per user" check here. A player
        # row still has at most one owner — guaranteed by the single user_id
        # column — so that needs no assertion either.

    print(f"users: {total_users} total. players: {total_players} total, "
          f"{claimed_players} now owned (user_id set).")
    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed.")


def main():
    if "--add-columns" in sys.argv:
        add_columns()
    if "--backfill" in sys.argv:
        backfill()
    if "--verify" in sys.argv:
        verify()
    if len(sys.argv) == 1:
        print(__doc__)


if __name__ == "__main__":
    main()
