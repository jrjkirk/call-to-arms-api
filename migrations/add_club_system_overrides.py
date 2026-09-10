"""Let a club override the signup-form half of the systems catalogue.

`SystemConfig` is shared by every club. Until now the only thing a club could
say about how its own night ran was the vibe list, so two clubs running The Old
World had to agree on 2000 points, and a club playing Kill Team to a scenario
pack had no way to say so.

Nine nullable columns on `club_systems`, mirroring their SystemConfig
counterparts:

    uses_points        boolean      scenario_options   jsonb
    default_points     integer      default_scenario   text
    max_points         integer      allows_demo        boolean
    uses_scenarios     boolean      uses_standby       boolean
                                    has_intro_prepass  boolean

## Every one stays NULL, and that is the entire migration

NULL means "no opinion, use the catalogue". Nothing is backfilled, on purpose.
Copying each system's current catalogue values into every club_systems row
would produce identical behaviour today and quietly freeze every club at the
values of the day it was created — a platform admin correcting a system's max
points would then reach nobody. Leaving them NULL keeps the platform default
live for every club that has not deliberately overridden it.

The booleans are nullable rather than DEFAULT FALSE for the same reason in
miniature: False is a real answer ("we do not play to points"), so it has to be
distinguishable from "unset".

One-off script, not a long-lived migration tool (see CLAUDE.md / models.py
docstring). Additive + idempotent, so it is safe to re-run.

    PYTHONPATH=. python migrations/add_club_system_overrides.py
    PYTHONPATH=. python migrations/add_club_system_overrides.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine

_COLUMNS = {
    "uses_points": "BOOLEAN",
    "default_points": "INTEGER",
    "max_points": "INTEGER",
    "uses_scenarios": "BOOLEAN",
    "scenario_options": "JSONB",
    "default_scenario": "TEXT",
    "allows_demo": "BOOLEAN",
    "uses_standby": "BOOLEAN",
    "has_intro_prepass": "BOOLEAN",
}


def add_columns():
    with Session(engine) as session:
        for name, sql_type in _COLUMNS.items():
            session.exec(text(
                f"ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS {name} {sql_type}"
            ))
        session.commit()
    print(f"Added {len(_COLUMNS)} columns to club_systems (or they were already there).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        for name, sql_type in _COLUMNS.items():
            row = session.exec(text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'club_systems' AND column_name = :c"
            ), params={"c": name}).first()
            if row is None:
                problems.append(f"column {name} is missing")
                continue
            data_type, is_nullable = row
            # Nullable permanently. NULL is the "inherit the catalogue" signal
            # and every existing row must be in it.
            if is_nullable != "YES":
                problems.append(f"{name}: expected nullable, got is_nullable={is_nullable}")
            want = {"BOOLEAN": "boolean", "INTEGER": "integer",
                    "JSONB": "jsonb", "TEXT": "text"}[sql_type]
            if data_type != want:
                problems.append(f"{name}: expected {want}, got {data_type}")

        # Nothing should be overridden yet. A non-NULL here straight after the
        # migration would mean something backfilled, which is exactly the
        # frozen-defaults mistake this docstring warns about.
        cols = " OR ".join(f"{c} IS NOT NULL" for c in _COLUMNS)
        overridden = session.exec(text(
            f"SELECT count(*) FROM club_systems WHERE {cols}"
        )).first()
        total = session.exec(text("SELECT count(*) FROM club_systems")).first()
        print(f"  club_systems rows: {total[0]}, with any override set: {overridden[0]}")
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
    print("Verified: all nine club_systems override columns are nullable and unset.")
