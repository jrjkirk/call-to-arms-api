"""New-club request form (2026-07-20): creates club_requests.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Brand-new table, same
create-if-not-exists pattern as create_platform_admin_tools.py.

Run manually (needs the repo root on PYTHONPATH):

    PYTHONPATH=. python migrations/create_club_requests_table.py
    PYTHONPATH=. python migrations/create_club_requests_table.py --verify-only

Safe to re-run: table creation uses SQLModel's checkfirst.
"""
import sys

from sqlmodel import Session, text

from database import engine
from models import ClubRequest


def create_table():
    ClubRequest.metadata.create_all(engine, tables=[ClubRequest.__table__], checkfirst=True)
    print("Created club_requests table (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    expected_cols = {
        "id", "created_at", "status", "requester_name", "requester_email",
        "club_name", "club_location", "notes", "reviewed_at",
        "reviewed_by_user_id", "reviewed_by_name",
    }
    with Session(engine) as session:
        exists = session.exec(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'club_requests'"
        )).first()
        if not exists:
            problems.append("club_requests table does not exist")
        else:
            cols = {row[0] for row in session.exec(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'club_requests'"
            )).all()}
            missing = expected_cols - cols
            if missing:
                problems.append(f"club_requests missing columns: {missing}")
    return problems


def main():
    if "--verify-only" not in sys.argv:
        create_table()
    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed: club_requests table present with expected columns.")


if __name__ == "__main__":
    main()
