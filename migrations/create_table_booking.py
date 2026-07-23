"""Table-booking feature, schema step: create the `table_booking_configs` and
`table_booking_notifications` tables, and add the `table_booking_enabled`
toggle to `club_systems`.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Same shape as
create_missions_and_flags.py: brand-new tables (checkfirst create_all) plus
an expand-style ALTER on an existing table. The toggle carries NOT NULL
DEFAULT FALSE, so existing club_systems rows get a valid value immediately —
no nullable window, no separate contract step.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/create_table_booking.py
    PYTHONPATH=. python migrations/create_table_booking.py --verify-only

Safe to re-run: table creation uses SQLModel's checkfirst; ADD COLUMN uses
IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine
from models import TableBookingConfig, TableBookingNotification


def create_tables():
    TableBookingConfig.metadata.create_all(
        engine, tables=[TableBookingConfig.__table__], checkfirst=True
    )
    TableBookingNotification.metadata.create_all(
        engine, tables=[TableBookingNotification.__table__], checkfirst=True
    )
    print("Created table_booking_configs + table_booking_notifications "
          "(or already present).")


def add_flag():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS "
            "table_booking_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        ))
    print("Added table_booking_enabled to club_systems (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        for table, expected_cols in (
            ("table_booking_configs", {
                "id", "club_id", "system_id", "venue_name", "venue_email",
                "cc_emails", "players_per_table", "include_player_names",
                "send_mode", "cutoff_day", "cutoff_time", "subject_template",
                "notes", "created_at", "updated_at",
            }),
            ("table_booking_notifications", {
                "id", "club_id", "system_id", "week", "tables", "headcount",
                "status", "error", "sent_at",
            }),
        ):
            exists = session.exec(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
            ).bindparams(t=table)).first()
            if not exists:
                problems.append(f"{table} table does not exist")
                continue
            cols = {row[0]: row[1] for row in session.exec(text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_name = :t"
            ).bindparams(t=table)).all()}
            missing = expected_cols - set(cols)
            if missing:
                problems.append(f"{table} missing columns: {missing}")
            for nn in ("club_id", "system_id"):
                if cols.get(nn) != "NO":
                    problems.append(f"{table}.{nn} should be NOT NULL (is_nullable={cols.get(nn)})")

        cs_cols = {row[0]: row[1] for row in session.exec(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'club_systems'"
        )).all()}
        if "table_booking_enabled" not in cs_cols:
            problems.append("club_systems missing column table_booking_enabled")
        elif cs_cols.get("table_booking_enabled") != "NO":
            problems.append(
                f"club_systems.table_booking_enabled should be NOT NULL "
                f"(is_nullable={cs_cols.get('table_booking_enabled')})"
            )

    return problems


def main():
    if "--verify-only" not in sys.argv:
        create_tables()
        add_flag()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: table_booking tables + club_systems toggle present.")


if __name__ == "__main__":
    main()
