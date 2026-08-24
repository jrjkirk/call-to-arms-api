"""Merge the duplicate Player rows created by the archive/identity bug.

Three people's records were split across five rows (see the note on
active_player_id_for in database.py for how). This repoints every dependent
row onto the survivor and removes the duplicate.

    Shaun Warne   47 (user 26, 16 signups)  <- 119 (user 69, 1 signup)
                                            <- 113 (DELETED by hand; its two
                                               signups, 747 and 775, are
                                               orphaned and point at nothing)
    Snoozi       114 (user 70, the original row, archived)
                                            <- 121 (user 70, 1 signup)

Survivor choice differs between the two on purpose. Shaun's survivor is the
row with the history. Snoozi's is the ORIGINAL row (114) even though it has no
signups, because it is the one his account first created — but it is currently
archived, so this reactivates it. Either way each user ends with exactly one
row and every game they've played hangs off it.

Shaun's manual "20 games played elsewhere" adjustment on 119 is dropped: he
entered it to compensate for the history he'd lost, and merging gives the real
games back. Leaving it would double-count him straight to Veteran.

Idempotent — rows already merged are skipped. Dry run by default.

    PYTHONPATH=. python migrations/merge_duplicate_players.py           # dry run
    PYTHONPATH=. python migrations/merge_duplicate_players.py --apply
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import (
    LeagueRating, LeagueResult, Player, PlayerDiscordVerification,
    PlayerExperienceAdjustment, PlayerLevelAnnouncement, Signup, User,
)

# (survivor_id, [duplicate_ids], reactivate_survivor)
MERGES = [
    (47, [119], False),
    (114, [121], True),
]

# Signups whose player row was deleted outright, and who they really belong to.
ORPHANS = {747: 47, 775: 47}

# Adjustments to drop after merging: entered to paper over lost history.
DROP_ADJUSTMENTS = [(119, "The Old World")]


def main(apply: bool) -> None:
    with Session(engine) as db:
        for survivor_id, dup_ids, reactivate in MERGES:
            survivor = db.get(Player, survivor_id)
            if survivor is None:
                print(f"!! survivor {survivor_id} missing — skipping")
                continue
            print(f"\n=== {survivor.name!r}: keeping player {survivor_id}")

            for dup_id in dup_ids:
                dup = db.get(Player, dup_id)
                if dup is None:
                    print(f"  player {dup_id} already gone")
                    continue
                if dup.club_id != survivor.club_id:
                    print(f"  !! player {dup_id} is at a different club — skipping")
                    continue

                moved = 0
                for s in db.exec(select(Signup).where(Signup.player_id == dup_id)).all():
                    s.player_id = survivor_id
                    s.player_name = survivor.name
                    db.add(s)
                    moved += 1
                print(f"  player {dup_id}: {moved} signup(s) -> {survivor_id}")

                for r in db.exec(select(LeagueResult).where(
                    (LeagueResult.player_1_id == dup_id) | (LeagueResult.player_2_id == dup_id)
                )).all():
                    if r.player_1_id == dup_id:
                        r.player_1_id, r.player_1_name = survivor_id, survivor.name
                    if r.player_2_id == dup_id:
                        r.player_2_id, r.player_2_name = survivor_id, survivor.name
                    db.add(r)
                    print(f"  league result {r.id} -> {survivor_id}")

                # Side tables: the survivor's own row always wins, so a
                # duplicate's entry is dropped rather than moved. Ratings and
                # level state are both derived and get rebuilt from the merged
                # games anyway.
                for model, col in (
                    (LeagueRating, LeagueRating.player_id),
                    (PlayerLevelAnnouncement, PlayerLevelAnnouncement.player_id),
                    (PlayerExperienceAdjustment, PlayerExperienceAdjustment.player_id),
                    (PlayerDiscordVerification, PlayerDiscordVerification.player_id),
                ):
                    for row in db.exec(select(model).where(col == dup_id)).all():
                        print(f"  drop {model.__name__} {row.id}")
                        db.delete(row)

                # The duplicate's account link moves to the survivor only if the
                # survivor has none — Shaun's 47 already belongs to user 26, and
                # his second Discord account shouldn't take it over.
                if dup.user_id is not None and survivor.user_id is None:
                    print(f"  survivor adopts user {dup.user_id}")
                    survivor.user_id = dup.user_id
                    db.add(survivor)

                for u in db.exec(select(User).where(User.player_id == dup_id)).all():
                    u.player_id = survivor_id if u.id == survivor.user_id else None
                    print(f"  user {u.id}.player_id -> {u.player_id}")
                    db.add(u)

                print(f"  delete player {dup_id} ({dup.name!r})")
                db.delete(dup)

            if reactivate and not survivor.active:
                print(f"  reactivating player {survivor_id}")
                survivor.active = True
                db.add(survivor)

        print("\n=== orphaned signups (player row deleted by hand)")
        for signup_id, owner_id in ORPHANS.items():
            s = db.get(Signup, signup_id)
            owner = db.get(Player, owner_id)
            if s is None or owner is None:
                print(f"  signup {signup_id}: missing — skipping")
                continue
            if s.player_id == owner_id:
                print(f"  signup {signup_id}: already attributed")
                continue
            if db.get(Player, s.player_id) is not None:
                print(f"  signup {signup_id}: player {s.player_id} exists — NOT an orphan, skipping")
                continue
            print(f"  signup {signup_id} ({s.week}) player {s.player_id} -> {owner_id} {owner.name!r}")
            s.player_id = owner_id
            s.player_name = owner.name
            db.add(s)

        print("\n=== adjustments to drop")
        for pid, system in DROP_ADJUSTMENTS:
            for a in db.exec(select(PlayerExperienceAdjustment).where(
                PlayerExperienceAdjustment.player_id == pid,
                PlayerExperienceAdjustment.system == system,
            )).all():
                print(f"  drop adjustment {a.id} (player {pid}, +{a.extra_games} games)")
                db.delete(a)

        # A player is meant to have at most one signup per (week, system) —
        # submit_signup's upsert relies on it. Merging two rows is the one
        # thing that could break that, if both signed up the same week.
        print("\n=== duplicate-week check")
        db.flush()
        clash = False
        for survivor_id, _, _ in MERGES:
            seen = {}
            for s in db.exec(select(Signup).where(Signup.player_id == survivor_id)).all():
                seen.setdefault((s.week, s.system), []).append(s.id)
            for key, sids in seen.items():
                if len(sids) > 1:
                    clash = True
                    print(f"  !! player {survivor_id} now has {len(sids)} signups for {key}: {sids}")
        if not clash:
            print("  clean — one signup per week/system for every survivor")
        elif apply:
            db.rollback()
            print("\nABORTED — merging would create duplicate signups. Resolve by hand first.")
            return

        if apply:
            db.commit()
            print("\nAPPLIED.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
