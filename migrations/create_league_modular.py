"""Modular leagues, schema step (expand only — no backfill, no data change).

Creates the two new tables (`league_seasons`, `league_configs`) and adds the
nullable FK columns (`system_id`, `season_id`) to `league_results` /
`league_ratings`, plus `club_systems.league_enabled`. The nullable columns are
backfilled + made NOT NULL by seed/seed_manchester_league.py afterwards (the
expand → backfill → contract pattern; see add_has_league_to_systems.py /
create_missions_and_flags.py).

Order matters: `league_seasons` must exist before the `season_id` FK columns
reference it, so tables are created first.

Run (staging then prod):
    PYTHONPATH=. python migrations/create_league_modular.py
    PYTHONPATH=. python migrations/create_league_modular.py --verify-only

Safe to re-run: CREATE via checkfirst; ADD COLUMN IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine
from models import LeagueConfig, LeagueSeason


def create_tables():
    LeagueSeason.metadata.create_all(engine, tables=[LeagueSeason.__table__], checkfirst=True)
    LeagueConfig.metadata.create_all(engine, tables=[LeagueConfig.__table__], checkfirst=True)
    print("Created league_seasons + league_configs (or already present).")


def add_columns():
    with engine.begin() as conn:
        for tbl in ("league_results", "league_ratings"):
            conn.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS system_id INTEGER REFERENCES systems(id)"
            ))
            conn.execute(text(
                f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS season_id INTEGER REFERENCES league_seasons(id)"
            ))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{tbl}_system_id ON {tbl}(system_id)"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{tbl}_season_id ON {tbl}(season_id)"))
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS league_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        ))
    print("Added system_id/season_id to league_results+league_ratings, "
          "league_enabled to club_systems (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        def cols(table):
            return {r[0]: r[1] for r in session.exec(text(
                f"SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='{table}'"
            )).all()}

        for table in ("league_seasons", "league_configs"):
            if not session.exec(text(
                f"SELECT 1 FROM information_schema.tables WHERE table_name='{table}'"
            )).first():
                problems.append(f"{table} table does not exist")

        for table in ("league_results", "league_ratings"):
            c = cols(table)
            for col in ("system_id", "season_id"):
                if col not in c:
                    problems.append(f"{table}.{col} missing")
        if "league_enabled" not in cols("club_systems"):
            problems.append("club_systems.league_enabled missing")

    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed: new league tables + columns present.")
    return problems


def main():
    if "--verify-only" not in sys.argv:
        create_tables()
        add_columns()
    verify()


if __name__ == "__main__":
    main()
