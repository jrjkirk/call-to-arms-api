"""Archive every player not linked to a Discord account.

Clears the roster down to people who actually have an account, so admin
pickers, signup and the league stop offering names nobody answers to. Only
`Player.active` changes: signups, results and everything derived from them
stay put, and an admin can restore anyone from the Players tab.

"Not linked" rather than "never logged in", because the second isn't knowable:
nothing records past ownership, and some of these rows (Test Mctest) did have
an account once. Nobody owns them now, so archiving takes nothing from anyone.

Run AFTER the claim change in auth.py is deployed. That change keeps archived,
unclaimed rows in the claim list and reactivates a row when it is claimed;
without it, a returning player can't find their row and creates a second one.

A row still pointed at by the legacy User.player_id link is left alone.

Idempotent. Dry run by default.

    PYTHONPATH=. python migrations/archive_unlinked_players.py           # dry run
    PYTHONPATH=. python migrations/archive_unlinked_players.py --apply
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import Player, User


def main(apply: bool) -> None:
    with Session(engine) as db:
        legacy = {u.player_id for u in db.exec(select(User).where(User.player_id.is_not(None))).all()}
        rows = db.exec(
            select(Player)
            .where(Player.user_id.is_(None), Player.active == True)
            .order_by(Player.club_id, Player.name)
        ).all()

        archived = 0
        for p in rows:
            if p.id in legacy:
                print(f"  skip {p.id} {p.name!r}: a user's legacy player_id still points here")
                continue
            print(f"  archive club {p.club_id} player {p.id} {p.name!r}")
            p.active = False
            db.add(p)
            archived += 1
        print(f"\n{archived} player(s) to archive")

        if apply:
            db.commit()
            print("APPLIED.")
        else:
            db.rollback()
            print("DRY RUN, nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
