"""Missions feature, schema step: create the `missions` table and add the two
per-club-system toggles (`missions_enabled`, `missions_use_secondary`) to
`club_systems`.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Combines a brand-new table
(like create_club_settings_table.py) with an expand-style ALTER on an existing
table (like add_has_league_to_systems.py). Both toggles carry NOT NULL DEFAULT
FALSE, so existing club_systems rows get a valid value immediately — no
nullable window, no separate contract step.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/create_missions_and_flags.py
    PYTHONPATH=. python migrations/create_missions_and_flags.py --verify-only

Safe to re-run: table creation uses SQLModel's checkfirst; ADD COLUMN uses
IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine
from models import Mission


def create_table():
    Mission.metadata.create_all(engine, tables=[Mission.__table__], checkfirst=True)
    # image_path/image_url are nullable (text-only missions like TOW's Open
    # Battle). A freshly created table gets this from the model; a table
    # created by an earlier revision of this script had them NOT NULL, so drop
    # that here (idempotent — a no-op if already nullable).
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE missions ALTER COLUMN image_path DROP NOT NULL"))
        conn.execute(text("ALTER TABLE missions ALTER COLUMN image_url DROP NOT NULL"))
    print("Created missions table (or already present); image columns nullable.")


def add_flags():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS "
            "missions_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS "
            "missions_use_secondary BOOLEAN NOT NULL DEFAULT FALSE"
        ))
    print("Added missions_enabled + missions_use_secondary to club_systems "
          "(or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        exists = session.exec(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'missions'"
        )).first()
        if not exists:
            problems.append("missions table does not exist")
        else:
            cols = {row[0]: row[1] for row in session.exec(text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'missions'"
            )).all()}
            expected = {"id", "club_id", "system_id", "name", "secondary_objectives",
                        "image_path", "image_url", "active", "created_at"}
            missing = expected - set(cols)
            if missing:
                problems.append(f"missions missing columns: {missing}")
            for nn in ("club_id", "system_id"):
                if cols.get(nn) != "NO":
                    problems.append(f"missions.{nn} should be NOT NULL (is_nullable={cols.get(nn)})")
            for nullable in ("image_path", "image_url"):
                if cols.get(nullable) != "YES":
                    problems.append(f"missions.{nullable} should be NULLABLE (is_nullable={cols.get(nullable)})")

        cs_cols = {row[0]: row[1] for row in session.exec(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'club_systems'"
        )).all()}
        for flag in ("missions_enabled", "missions_use_secondary"):
            if flag not in cs_cols:
                problems.append(f"club_systems missing column {flag}")
            elif cs_cols.get(flag) != "NO":
                problems.append(f"club_systems.{flag} should be NOT NULL (is_nullable={cs_cols.get(flag)})")

    return problems


def main():
    if "--verify-only" not in sys.argv:
        create_table()
        add_flags()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: missions table + club_systems toggles present.")


if __name__ == "__main__":
    main()
