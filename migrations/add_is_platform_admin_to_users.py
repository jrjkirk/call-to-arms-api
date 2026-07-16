"""Phase 2 kickoff: add is_platform_admin to users.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Same pattern as the
add_club_id_to_*.py scripts, but single-step: unlike club_id, every existing
user is unambiguously not a platform admin, so this goes straight to
NOT NULL DEFAULT false with no separate backfill/contract phase.

Run manually:

    python add_is_platform_admin_to_users.py --add-column
    python add_is_platform_admin_to_users.py --verify

Safe to re-run: ALTER TABLE uses IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import User


def add_column():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_platform_admin boolean NOT NULL DEFAULT false"
        ))
    print("Added is_platform_admin column to users (or already present).")


def verify():
    with Session(engine) as session:
        total = len(session.exec(select(User)).all())
        non_platform_admin = len(session.exec(
            select(User).where(User.is_platform_admin == False)
        ).all())

    problems = []
    if non_platform_admin != total:
        problems.append(
            f"only {non_platform_admin} of {total} row(s) have is_platform_admin = false"
        )

    print(f"users: {total} total row(s), {non_platform_admin} with is_platform_admin=false")
    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed.")


def main():
    if "--add-column" in sys.argv:
        add_column()
    if "--verify" in sys.argv:
        verify()
    if len(sys.argv) == 1:
        print(__doc__)


if __name__ == "__main__":
    main()
