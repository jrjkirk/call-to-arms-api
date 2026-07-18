"""Modular leagues, backfill step: migrate Manchester's live Old World league
onto the per-(club, system, season) structure WITHOUT changing any ratings.

Steps (idempotent where possible):
  1. Ensure a LeagueConfig for (Manchester, The Old World) with the exact
     original ELO defaults (K 10/40, painting +3/+1, start 1000).
  2. Ensure an open LeagueSeason (start = earliest existing result date, end =
     NULL/ongoing) so every existing result falls inside it.
  3. Backfill system_id/season_id on every existing league_results +
     league_ratings row.
  4. **Safety gate:** snapshot every player's current rating, run the
     refactored recalc, and assert the rebuilt ratings are byte-identical
     (within 1e-9). Aborts before contract if anything differs.
  5. Set ClubSystem(Manchester, TOW).league_enabled = True.
  6. SET NOT NULL on the backfilled columns (only when zero NULLs remain).

Run against whichever DB the current env points at (staging, then prod):
    PYTHONPATH=. python seed/seed_manchester_league.py
    PYTHONPATH=. python seed/seed_manchester_league.py --verify-only
"""
import sys
from datetime import date, datetime

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from league import _recalculate_ratings
from models import Club, ClubSystem, LeagueConfig, LeagueRating, LeagueResult, LeagueSeason, SystemConfig

TOW = "The Old World"
EPS = 1e-9


def _parse_result_date(s: str):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _resolve(db: Session):
    club = db.exec(select(Club).where(Club.slug == "manchester")).first()
    if club is None:
        raise RuntimeError("No Manchester club (slug='manchester').")
    tow = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW)).first()
    if tow is None:
        raise RuntimeError("No The Old World system.")
    cs = db.exec(select(ClubSystem).where(
        ClubSystem.club_id == club.id, ClubSystem.system_id == tow.id)).first()
    if cs is None:
        raise RuntimeError("Manchester does not run The Old World.")
    return club, tow, cs


def run():
    with Session(engine) as db:
        club, tow, cs = _resolve(db)

        # 1. config
        cfg = db.exec(select(LeagueConfig).where(
            LeagueConfig.club_id == club.id, LeagueConfig.system_id == tow.id)).first()
        if cfg is None:
            cfg = LeagueConfig(club_id=club.id, system_id=tow.id)  # code defaults == original ELO
            db.add(cfg); db.commit(); db.refresh(cfg)
            print("Created LeagueConfig (default ELO) for Manchester TOW.")
        else:
            print("LeagueConfig already present.")

        # 2. season
        results = db.exec(select(LeagueResult).where(LeagueResult.club_id == club.id)).all()
        season = db.exec(select(LeagueSeason).where(
            LeagueSeason.club_id == club.id, LeagueSeason.system_id == tow.id)).first()
        if season is None:
            dates = [d for d in (_parse_result_date(r.result_date) for r in results) if d]
            start = min(dates) if dates else date.today()
            season = LeagueSeason(club_id=club.id, system_id=tow.id,
                                  name=str(start.year), start_date=start, end_date=None)
            db.add(season); db.commit(); db.refresh(season)
            print(f"Created LeagueSeason {season.name!r} (start {start}, open) for Manchester TOW.")
        else:
            print(f"LeagueSeason already present ({season.name}).")

        # 3. backfill system_id/season_id on results + ratings
        n_res = n_rat = 0
        for r in results:
            if r.system_id is None or r.season_id is None:
                r.system_id, r.season_id = tow.id, season.id
                db.add(r); n_res += 1
        for rt in db.exec(select(LeagueRating).where(LeagueRating.club_id == club.id)).all():
            if rt.system_id is None or rt.season_id is None:
                rt.system_id, rt.season_id = tow.id, season.id
                db.add(rt); n_rat += 1
        db.commit()
        print(f"Backfilled {n_res} result(s) + {n_rat} rating(s) to system={tow.id}, season={season.id}.")

        # 4. SAFETY GATE — snapshot ratings, recalc, compare
        before = {rt.player_id: rt.rating for rt in db.exec(
            select(LeagueRating).where(LeagueRating.club_id == club.id)).all()}
        _recalculate_ratings(db, club.id, tow.id, season.id)
        db.commit()
        after = {rt.player_id: rt.rating for rt in db.exec(
            select(LeagueRating).where(
                LeagueRating.club_id == club.id,
                LeagueRating.system_id == tow.id,
                LeagueRating.season_id == season.id)).all()}

        problems = []
        if set(before) != set(after):
            problems.append(f"player set changed: before={sorted(before)} after={sorted(after)}")
        for pid in set(before) & set(after):
            if abs(before[pid] - after[pid]) > EPS:
                problems.append(f"player {pid}: {before[pid]} -> {after[pid]} (drift)")
        if problems:
            print("\n*** SAFETY GATE FAILED — recalc changed the ratings. NOT contracting. ***")
            for p in problems:
                print(f"  - {p}")
            db.rollback()
            sys.exit(1)
        print(f"SAFETY GATE PASSED: {len(after)} rating(s) reproduced byte-identically.")

        # 5. enable league for this club-system
        cs.league_enabled = True
        db.add(cs); db.commit()
        print("Set Manchester TOW league_enabled = True.")

    # 6. contract (guarded)
    _contract()


def _contract():
    with engine.begin() as conn:
        for tbl in ("league_results", "league_ratings"):
            nulls = conn.execute(text(
                f"SELECT COUNT(*) FROM {tbl} WHERE system_id IS NULL OR season_id IS NULL"
            )).scalar()
            if nulls:
                print(f"Skipping NOT NULL on {tbl}: {nulls} row(s) still NULL "
                      f"(other clubs not yet migrated?).")
                continue
            conn.execute(text(f"ALTER TABLE {tbl} ALTER COLUMN system_id SET NOT NULL"))
            conn.execute(text(f"ALTER TABLE {tbl} ALTER COLUMN season_id SET NOT NULL"))
            print(f"{tbl}.system_id + season_id are now NOT NULL.")


def verify():
    with Session(engine) as db:
        club, tow, cs = _resolve(db)
        n = len(db.exec(select(LeagueResult).where(
            LeagueResult.club_id == club.id, LeagueResult.system_id.is_(None))).all())
        seasons = db.exec(select(LeagueSeason).where(LeagueSeason.club_id == club.id)).all()
        cfg = db.exec(select(LeagueConfig).where(LeagueConfig.club_id == club.id)).first()
        print(f"Manchester: league_enabled={cs.league_enabled}, seasons={len(seasons)}, "
              f"config={'yes' if cfg else 'no'}, results-missing-system_id={n}")


def main():
    if "--verify-only" in sys.argv:
        verify()
    else:
        run()
        verify()


if __name__ == "__main__":
    main()
