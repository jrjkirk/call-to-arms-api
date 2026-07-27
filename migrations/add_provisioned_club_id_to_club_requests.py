"""Add `provisioned_club_id` to `club_requests` (one-click club provisioning).

Nullable FK to clubs.id: set when a platform admin provisions a request into a
real Club, NULL otherwise. One-off script, not a long-lived migration tool
(see CLAUDE.md / models.py docstring). Additive + idempotent — safe to re-run.

Run manually (from repo root, so repo-root imports resolve):

    PYTHONPATH=. python migrations/add_provisioned_club_id_to_club_requests.py
    PYTHONPATH=. python migrations/add_provisioned_club_id_to_club_requests.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_column():
    with Session(engine) as session:
        session.exec(text(
            "ALTER TABLE club_requests "
            "ADD COLUMN IF NOT EXISTS provisioned_club_id INTEGER REFERENCES clubs(id)"
        ))
        session.exec(text(
            "CREATE INDEX IF NOT EXISTS ix_club_requests_provisioned_club_id "
            "ON club_requests (provisioned_club_id)"
        ))
        # Root-cause fix for a latent bug this feature trips over: clubs has an
        # orphaned NOT-NULL `leagues_enabled` column (retired from the model
        # 2026-07-19) with no DB default, so ANY ORM insert of a Club — the
        # existing POST /admin/platform/clubs as well as the new provision
        # endpoint — omits it and hits a NotNullViolation. Give it a default so
        # ORM inserts succeed; if the column was already dropped, skip quietly.
        col = session.exec(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'clubs' AND column_name = 'leagues_enabled'"
        )).first()
        if col is not None:
            session.exec(text("ALTER TABLE clubs ALTER COLUMN leagues_enabled SET DEFAULT false"))
            session.exec(text("UPDATE clubs SET leagues_enabled = false WHERE leagues_enabled IS NULL"))
        session.commit()
    print("Added club_requests.provisioned_club_id + defaulted clubs.leagues_enabled (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        row = session.exec(text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'club_requests' AND column_name = 'provisioned_club_id'"
        )).first()
        if row is None:
            problems.append("column provisioned_club_id is missing")
        else:
            data_type, is_nullable = row
            if is_nullable != "YES":
                problems.append(f"expected nullable column, got is_nullable={is_nullable}")
            if data_type != "integer":
                problems.append(f"expected integer, got {data_type}")

        fk = session.exec(text(
            """
            SELECT 1
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'club_requests'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'provisioned_club_id'
            """
        )).first()
        if fk is None:
            problems.append("expected FK on provisioned_club_id -> clubs(id)")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        add_column()
    problems = verify()
    if problems:
        print("VERIFY FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("VERIFY OK: club_requests.provisioned_club_id present, nullable integer, FK to clubs.")
