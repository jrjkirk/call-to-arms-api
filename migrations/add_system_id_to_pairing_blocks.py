"""Add nullable `system_id` to `pairing_blocks` (per-system pairing blocks).

A block used to mean one thing: these two never get paired, anywhere in the
club. That is still what most of them mean — two people who travel together,
or who have asked not to be drawn against each other — and every existing row
keeps meaning exactly that.

What it could not say is the other reason a block gets added: "these two have
met four times in Kill Team", which has no bearing on their Old World games.
That is a judgement the system's own admin makes, so the block needs to be
able to belong to one system.

  system_id IS NULL   club-wide. Every pre-existing row, and the default.
  system_id = <id>    applies only when pairing that system.

Nullable on purpose and permanently: NULL is a meaning here, not a
backfill-in-progress. Do not make this NOT NULL later.

The matcher reads it in pairings_engine.generate() and drops blocks belonging
to a different system before building its lookup set. A club that never scopes
a block pairs exactly as it did before, which is the property that made this
safe to add to a frozen engine.

One-off script, not a long-lived migration tool (see CLAUDE.md / models.py
docstring). Additive + idempotent, so it is safe to re-run.

Run manually (from repo root, so repo-root imports resolve):

    PYTHONPATH=. python migrations/add_system_id_to_pairing_blocks.py
    PYTHONPATH=. python migrations/add_system_id_to_pairing_blocks.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_column():
    with Session(engine) as session:
        session.exec(text(
            "ALTER TABLE pairing_blocks "
            "ADD COLUMN IF NOT EXISTS system_id INTEGER REFERENCES systems(id)"
        ))
        # Every lookup is "this club's blocks for this system, plus its
        # club-wide ones", and the matcher does it on every pairing run.
        session.exec(text(
            "CREATE INDEX IF NOT EXISTS ix_pairing_blocks_system_id "
            "ON pairing_blocks (system_id)"
        ))
        session.commit()
    print("Added pairing_blocks.system_id (or it was already there).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        row = session.exec(text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'pairing_blocks' AND column_name = 'system_id'"
        )).first()
        if row is None:
            problems.append("column system_id is missing")
        else:
            data_type, is_nullable = row
            # NOT NULL here would silently turn every club-wide block into a
            # block on whichever system won the backfill, so it is checked.
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
            WHERE tc.table_name = 'pairing_blocks'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'system_id'
            """
        )).first()
        if fk is None:
            problems.append("expected FK on system_id -> systems(id)")

        idx = session.exec(text(
            "SELECT 1 FROM pg_indexes WHERE tablename = 'pairing_blocks' "
            "AND indexname = 'ix_pairing_blocks_system_id'"
        )).first()
        if idx is None:
            problems.append("expected index ix_pairing_blocks_system_id")

        # Existing rows must still be club-wide. If this ever reports a count,
        # something backfilled the column and every one of those blocks has
        # quietly stopped applying to the rest of the club's systems.
        scoped = session.exec(text(
            "SELECT count(*) FROM pairing_blocks WHERE system_id IS NOT NULL"
        )).first()
        print(f"  blocks scoped to a system: {scoped[0] if scoped else 0}")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        add_column()
    issues = verify()
    if issues:
        print("VERIFY FAILED:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("Verified: pairing_blocks.system_id is nullable, indexed, FK to systems(id).")
