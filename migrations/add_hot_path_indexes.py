"""Composite indexes for the hot multi-tenant query paths.

The weekly signup/pairing flow filters signups, pairings, and publish_state by
(club_id, system, week) — all equality — on nearly every read. Single-column
indexes exist, but a composite keeps these fast as rows accumulate across many
clubs. Additive + idempotent, no behaviour change. One-off script, not a live
migration tool (see CLAUDE.md / models.py docstring).

Tables are small today so a plain CREATE INDEX is instant and fine; if these
ever grow large, swap to CREATE INDEX CONCURRENTLY (which can't run inside a
transaction) to avoid a write lock.

Run (from repo root):
    PYTHONPATH=. python migrations/add_hot_path_indexes.py
    PYTHONPATH=. python migrations/add_hot_path_indexes.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine

INDEXES = {
    "ix_signups_club_system_week": "signups (club_id, system, week)",
    "ix_pairings_club_system_week": "pairings (club_id, system, week)",
    "ix_publish_state_club_system_week": "publish_state (club_id, system, week)",
}


def add_indexes():
    with Session(engine) as session:
        for name, target in INDEXES.items():
            session.exec(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))
        session.commit()
    print(f"Created {len(INDEXES)} composite index(es) (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        for name in INDEXES:
            found = session.exec(
                text("SELECT 1 FROM pg_indexes WHERE indexname = :n").bindparams(n=name)
            ).first()
            if found is None:
                problems.append(f"missing index {name}")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        add_indexes()
    problems = verify()
    if problems:
        print("VERIFY FAILED:")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("VERIFY OK: all composite hot-path indexes present.")
