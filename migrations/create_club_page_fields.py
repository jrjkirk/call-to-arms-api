"""Club landing page feature, schema step: adds the club-profile fields to
`clubs`, the carousel fields to `club_systems`, and creates the `club_events`
table.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Same combined shape as
create_missions_and_flags.py (new table + ALTER on existing tables). All new
columns are nullable (or carry a safe default) — no separate contract step.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/create_club_page_fields.py
    PYTHONPATH=. python migrations/create_club_page_fields.py --verify-only

Safe to re-run: table creation uses SQLModel's checkfirst; ADD COLUMN uses
IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine
from models import ClubEvent


def add_club_columns():
    with engine.begin() as conn:
        for col_sql in [
            "blurb TEXT",
            "logo_path TEXT",
            "logo_url TEXT",
            "website_url TEXT",
            "discord_url TEXT",
            "opening_hours JSON",
        ]:
            col_name = col_sql.split()[0]
            conn.execute(text(f"ALTER TABLE clubs ADD COLUMN IF NOT EXISTS {col_sql}"))
    print("Added club-profile columns to clubs (or already present).")


def add_club_system_columns():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS carousel_blurb TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS carousel_photo_path TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS carousel_photo_url TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS accent_color TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS carousel_order INTEGER NOT NULL DEFAULT 0"
        ))
    print("Added carousel columns to club_systems (or already present).")


def create_club_events_table():
    ClubEvent.metadata.create_all(engine, tables=[ClubEvent.__table__], checkfirst=True)
    print("Created club_events table (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        clubs_cols = {row[0] for row in session.exec(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'clubs'"
        )).all()}
        for col in ("blurb", "logo_path", "logo_url", "website_url", "discord_url", "opening_hours"):
            if col not in clubs_cols:
                problems.append(f"clubs missing column {col}")

        cs_cols = {row[0]: row[1] for row in session.exec(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'club_systems'"
        )).all()}
        for col in ("carousel_blurb", "carousel_photo_path", "carousel_photo_url", "accent_color"):
            if col not in cs_cols:
                problems.append(f"club_systems missing column {col}")
        if "carousel_order" not in cs_cols:
            problems.append("club_systems missing column carousel_order")
        elif cs_cols.get("carousel_order") != "NO":
            problems.append("club_systems.carousel_order should be NOT NULL")

        exists = session.exec(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'club_events'"
        )).first()
        if not exists:
            problems.append("club_events table does not exist")
        else:
            ce_cols = {row[0]: row[1] for row in session.exec(text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'club_events'"
            )).all()}
            expected = {"id", "club_id", "system_id", "title", "description",
                        "event_date", "start_time", "end_time", "all_day", "created_at"}
            missing = expected - set(ce_cols)
            if missing:
                problems.append(f"club_events missing columns: {missing}")
            for nn in ("club_id", "title", "event_date"):
                if ce_cols.get(nn) != "NO":
                    problems.append(f"club_events.{nn} should be NOT NULL (is_nullable={ce_cols.get(nn)})")
            if ce_cols.get("system_id") != "YES":
                problems.append("club_events.system_id should be NULLABLE")

    return problems


def main():
    if "--verify-only" not in sys.argv:
        add_club_columns()
        add_club_system_columns()
        create_club_events_table()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: club page fields + club_events table present.")


if __name__ == "__main__":
    main()
