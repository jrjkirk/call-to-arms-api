"""Platform admin tools (2026-07-20): creates platform_banner,
scheduled_job_runs, and audit_log_entries.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Three brand-new tables,
same create-table-if-not-exists pattern as create_club_settings_table.py /
create_missions_and_flags.py — no expand/contract needed, nothing to
backfill.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/create_platform_admin_tools.py
    PYTHONPATH=. python migrations/create_platform_admin_tools.py --verify-only

Safe to re-run: table creation uses SQLModel's checkfirst.
"""
import sys

from sqlmodel import Session, text

from database import engine
from models import AuditLogEntry, PlatformBanner, ScheduledJobRun


def create_tables():
    PlatformBanner.metadata.create_all(engine, tables=[PlatformBanner.__table__], checkfirst=True)
    ScheduledJobRun.metadata.create_all(engine, tables=[ScheduledJobRun.__table__], checkfirst=True)
    AuditLogEntry.metadata.create_all(engine, tables=[AuditLogEntry.__table__], checkfirst=True)
    print("Created platform_banner, scheduled_job_runs, audit_log_entries (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        for table, expected_cols in {
            "platform_banner": {"id", "message", "severity", "active", "updated_at"},
            "scheduled_job_runs": {"id", "job_name", "ran_at", "status", "detail"},
            "audit_log_entries": {"id", "created_at", "actor_user_id", "actor_name", "action", "target_type", "target_id", "detail"},
        }.items():
            exists = session.exec(text(
                "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
            ).bindparams(t=table)).first()
            if not exists:
                problems.append(f"{table} table does not exist")
                continue
            cols = {row[0] for row in session.exec(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
            ).bindparams(t=table)).all()}
            missing = expected_cols - cols
            if missing:
                problems.append(f"{table} missing columns: {missing}")
    return problems


def main():
    if "--verify-only" not in sys.argv:
        create_tables()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: all three tables present with expected columns.")


if __name__ == "__main__":
    main()
