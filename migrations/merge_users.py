"""Merge one account into another, by hand (account overhaul Slab 1).

For one person with two User rows, typically two Discord accounts. Everything
on --drop moves to --keep and --drop is deleted; see user_merge.py for exactly
what moves and what makes it refuse. Keep the account the person signs in with
now, or the one holding their player and history if they can switch.

Dry run by default: the merge runs inside a transaction and is rolled back, so
the output is exactly what --apply would do.

    PYTHONPATH=. python migrations/merge_users.py --keep 69 --drop 26            # dry run
    PYTHONPATH=. python migrations/merge_users.py --keep 69 --drop 26 --apply

Needs Slab 0 (user_identities) deployed and migrated.
"""
import argparse

from sqlmodel import Session, select

from database import engine
from models import Player, User, UserIdentity
from user_merge import MergeRefused, merge_users


def describe(db: Session, user_id: int) -> str:
    u = db.get(User, user_id)
    if u is None:
        return f"user {user_id}: does not exist"
    idents = [f"{i.provider}:{i.provider_user_id}" for i in
              db.exec(select(UserIdentity).where(UserIdentity.user_id == u.id)).all()]
    players = [f"{p.name!r} #{p.id} (club {p.club_id})" for p in
               db.exec(select(Player).where(Player.user_id == u.id)).all()]
    return (f"user {u.id} {u.discord_name!r} club={u.club_id} super={u.is_super_admin} "
            f"last_login={u.last_login_at:%d/%m/%Y}\n    identities: {idents or 'none'}"
            f"\n    players: {players or 'none'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, required=True)
    ap.add_argument("--drop", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with Session(engine) as db:
        print("KEEP " + describe(db, args.keep))
        print("DROP " + describe(db, args.drop))
        try:
            lines = merge_users(db, args.keep, args.drop)
        except MergeRefused as e:
            db.rollback()
            print("\nREFUSED, nothing written:")
            for p in e.problems:
                print(f"  - {p}")
            raise SystemExit(1)
        print()
        for line in lines:
            print(f"  {line}")
        if args.apply:
            db.commit()
            print("\nAPPLIED.")
        else:
            db.rollback()
            print("\nDRY RUN, nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main()
