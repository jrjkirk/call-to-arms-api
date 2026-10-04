"""Add the single-K and painting on/off options to `league_configs`.

Three new columns:

    single_k          one K value for every game, instead of casual/competitive
    k_single          that value (32 until a club changes it)
    painting_enabled  whether painting bonuses count at all

`painting_enabled` replaces `winloss_use_painting`, which only covered the flat
win/loss method: an ELO league had no way to switch painting off short of
setting both bonuses to zero, and its result form went on asking for them. The
old column is left in place, unread.

The backfill keeps every existing league scoring exactly as it does today:

    ELO leagues        painting_enabled = TRUE   (painting always applied)
    win/loss leagues   painting_enabled = winloss_use_painting

It runs only when this script adds the column. Re-running it later must not
overwrite a choice a club has made since.

## Run this BEFORE deploying the code that reads the columns

Additive, so the usual order: the running image ignores columns it doesn't
know, but the new image selects all three and fails on a table without them.

    PYTHONPATH=. python migrations/add_league_config_options.py
    PYTHONPATH=. python migrations/add_league_config_options.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine

COLUMNS = {
    "single_k": "BOOLEAN NOT NULL DEFAULT FALSE",
    "k_single": "INTEGER NOT NULL DEFAULT 32",
    "painting_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
}


def _has_column(session: Session, column: str) -> bool:
    return session.exec(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'league_configs' AND column_name = :c"
    ), params={"c": column}).first() is not None


def add_columns():
    with Session(engine) as session:
        painting_is_new = not _has_column(session, "painting_enabled")
        for column, ddl in COLUMNS.items():
            session.exec(text(f"ALTER TABLE league_configs ADD COLUMN IF NOT EXISTS {column} {ddl}"))
        if painting_is_new:
            result = session.exec(text(
                "UPDATE league_configs SET painting_enabled = winloss_use_painting "
                "WHERE scoring_method = 'winloss'"
            ))
            print(f"Backfilled painting_enabled on {result.rowcount} win/loss league(s).")
        session.commit()
    print("Added single_k, k_single, painting_enabled (or they were already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        for column in COLUMNS:
            if not _has_column(session, column):
                problems.append(f"{column} is missing")
        if problems:
            return problems
        rows = session.exec(text(
            "SELECT club_id, system_id, scoring_method, single_k, k_single, painting_enabled "
            "FROM league_configs ORDER BY club_id, system_id"
        )).all()
        for r in rows:
            print(f"  club {r[0]} system {r[1]}: {r[2]}, single_k={r[3]} (k={r[4]}), painting={r[5]}")
            # An ELO league applied painting unconditionally before this, and no
            # league used a single K. Anything else on a first run is a backfill
            # that changed how a live league scores.
            if r[3]:
                problems.append(f"club {r[0]} system {r[1]} has single_k set")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        add_columns()
    issues = verify()
    if issues:
        print("VERIFY FAILED:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("Verified.")
