"""Merge Ian's old roster row into the one his Discord account created.

    Ian   122 (user 76 "Iantm", 3 league results, 1 signup)
            <- 10 "Ian T-M"      (no account, 5 signups, 2 titles)
            <- 30 "Ian Tyrrell"  (no account, 3 signups)

Players 10 and 30 were both roster rows for the same person, never claimed, so
when Ian signed in on 25/08 he created a fresh profile (122) and started playing
league games on it. 122 survives because it holds the league standings and the
Discord link; the other rows' game history and titles move onto it, then it
takes the name "Ian T-M".

Level announcements are MOVED, not dropped as in merge_duplicate_players.py:
122 has none of its own, and without the old rows' high-water marks the next publish
would re-announce every level Ian had already reached.

Idempotent. Dry run by default.

    PYTHONPATH=. python migrations/merge_ian_players.py           # dry run
    PYTHONPATH=. python migrations/merge_ian_players.py --apply
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import (
    CallOut, LeagueRating, LeagueResult, PairingBlock, Player,
    PlayerDiscordVerification, PlayerExperienceAdjustment,
    PlayerLevelAnnouncement, Signup, TournamentEntry, User, VenueBooking,
)
from services import player_titles, set_player_titles

SURVIVOR_ID = 122
DUPLICATE_IDS = [10, 30]
FINAL_NAME = "Ian T-M"
EXPECTED_OWNER = 76


def main(apply: bool) -> None:
    with Session(engine) as db:
        survivor = db.get(Player, SURVIVOR_ID)
        if survivor is None:
            print(f"!! survivor {SURVIVOR_ID} missing")
            return
        print(f"survivor {survivor.id} {survivor.name!r} user={survivor.user_id} club={survivor.club_id}")
        if survivor.user_id != EXPECTED_OWNER:
            print(f"!! survivor is owned by user {survivor.user_id}, expected {EXPECTED_OWNER}. Stopping.")
            return

        for dup_id in DUPLICATE_IDS:
            dup = db.get(Player, dup_id)
            if dup is None:
                print(f"player {dup_id} already gone, nothing to merge")
                continue
            print(f"duplicate {dup.id} {dup.name!r} user={dup.user_id} club={dup.club_id}")
            if dup.club_id != survivor.club_id or dup.user_id is not None:
                print("!! duplicate is at another club or owned by an account. Stopping.")
                return

            for s in db.exec(select(Signup).where(Signup.player_id == dup_id)).all():
                print(f"  signup {s.id} {s.system} {s.week} -> {SURVIVOR_ID}")
                s.player_id = SURVIVOR_ID
                db.add(s)

            for r in db.exec(select(LeagueResult).where(
                (LeagueResult.player_1_id == dup_id) | (LeagueResult.player_2_id == dup_id)
            )).all():
                if r.player_1_id == dup_id:
                    r.player_1_id = SURVIVOR_ID
                if r.player_2_id == dup_id:
                    r.player_2_id = SURVIVOR_ID
                print(f"  league result {r.id} -> {SURVIVOR_ID}")
                db.add(r)

            titles = player_titles(survivor)
            for t in player_titles(dup):
                if t not in titles:
                    titles.append(t)
            if titles != player_titles(survivor):
                print(f"  titles -> {titles}")
                set_player_titles(survivor, titles)
                db.add(survivor)

            have = {a.system: a for a in db.exec(select(PlayerLevelAnnouncement).where(
                PlayerLevelAnnouncement.player_id == SURVIVOR_ID)).all()}
            for a in db.exec(select(PlayerLevelAnnouncement).where(
                PlayerLevelAnnouncement.player_id == dup_id)).all():
                mine = have.get(a.system)
                if mine is None:
                    print(f"  level announcement {a.id} ({a.system} L{a.last_level}) -> {SURVIVOR_ID}")
                    a.player_id = SURVIVOR_ID
                    db.add(a)
                else:
                    if a.last_level > mine.last_level:
                        print(f"  level announcement {mine.id} ({a.system}) L{mine.last_level} -> L{a.last_level}")
                        mine.last_level = a.last_level
                        db.add(mine)
                    print(f"  drop level announcement {a.id}")
                    db.delete(a)

            # Everything else was empty for players 10 and 30 when checked on
            # 15/09/2026. Refuse rather than guess if that has changed.
            leftovers = []
            for model, cond in (
                (LeagueRating, LeagueRating.player_id == dup_id),
                (PlayerExperienceAdjustment, PlayerExperienceAdjustment.player_id == dup_id),
                (PlayerDiscordVerification, PlayerDiscordVerification.player_id == dup_id),
                (PairingBlock, (PairingBlock.player_a_id == dup_id) | (PairingBlock.player_b_id == dup_id)),
                (CallOut, (CallOut.creator_player_id == dup_id) | (CallOut.taker_player_id == dup_id)),
                (VenueBooking, VenueBooking.player_id == dup_id),
                (TournamentEntry, TournamentEntry.player_id == dup_id),
                (User, User.player_id == dup_id),
            ):
                n = len(db.exec(select(model).where(cond)).all())
                if n:
                    leftovers.append(f"{model.__name__}={n}")
            if leftovers:
                db.rollback()
                print(f"!! player {dup_id} has rows this script does not handle: {leftovers}. Stopping.")
                return

            db.flush()
            print(f"  delete player {dup_id} ({dup.name!r})")
            db.delete(dup)
            db.flush()

        # Rename, and carry the name onto the denormalised copies so history
        # and league tables don't show "Ian" beside "Ian T-M".
        if survivor.name != FINAL_NAME:
            print(f"\nrename player {SURVIVOR_ID} {survivor.name!r} -> {FINAL_NAME!r}")
            survivor.name = FINAL_NAME
            db.add(survivor)
        renamed = 0
        for s in db.exec(select(Signup).where(Signup.player_id == SURVIVOR_ID)).all():
            if s.player_name != FINAL_NAME:
                s.player_name = FINAL_NAME; db.add(s); renamed += 1
        for r in db.exec(select(LeagueResult).where(
            (LeagueResult.player_1_id == SURVIVOR_ID) | (LeagueResult.player_2_id == SURVIVOR_ID)
        )).all():
            if r.player_1_id == SURVIVOR_ID and r.player_1_name != FINAL_NAME:
                r.player_1_name = FINAL_NAME; db.add(r); renamed += 1
            if r.player_2_id == SURVIVOR_ID and r.player_2_name != FINAL_NAME:
                r.player_2_name = FINAL_NAME; db.add(r); renamed += 1
        for r in db.exec(select(LeagueRating).where(LeagueRating.player_id == SURVIVOR_ID)).all():
            if r.player_name != FINAL_NAME:
                r.player_name = FINAL_NAME; db.add(r); renamed += 1
        for c in db.exec(select(CallOut).where(
            (CallOut.creator_player_id == SURVIVOR_ID) | (CallOut.taker_player_id == SURVIVOR_ID)
        )).all():
            if c.creator_player_id == SURVIVOR_ID and c.creator_name != FINAL_NAME:
                c.creator_name = FINAL_NAME; db.add(c); renamed += 1
            if c.taker_player_id == SURVIVOR_ID and c.taker_name != FINAL_NAME:
                c.taker_name = FINAL_NAME; db.add(c); renamed += 1
        print(f"  {renamed} stored name(s) updated")

        print("\n=== duplicate-week check")
        db.flush()
        seen = {}
        for s in db.exec(select(Signup).where(Signup.player_id == SURVIVOR_ID)).all():
            seen.setdefault((s.week, s.system), []).append(s.id)
        clashes = {k: v for k, v in seen.items() if len(v) > 1}
        if clashes:
            print(f"  !! {clashes}")
            db.rollback()
            print("\nABORTED: merging would create duplicate signups.")
            return
        print(f"  clean, {len(seen)} signup(s)")

        if apply:
            db.commit()
            print("\nAPPLIED.")
        else:
            db.rollback()
            print("\nDRY RUN, nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
