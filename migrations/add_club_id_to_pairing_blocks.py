"""Phase 1, table 1 of 10 (pairing_blocks): expand/backfill/dual-run steps for
adding club_id to pairing_blocks.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). `pairing_blocks` already
exists as a live table, so SQLModel's `create_all()` won't alter it; this
script runs the ALTER TABLE by hand, then backfills existing rows to
Manchester (the only club today).

Run manually, in order:

    python add_club_id_to_pairing_blocks.py --add-column   # expand: add nullable column + index
    python add_club_id_to_pairing_blocks.py --backfill      # backfill existing rows to Manchester
    python add_club_id_to_pairing_blocks.py --verify        # check: no NULLs, column present

The contract step (SET NOT NULL) is deliberately not in this script — it's
run by hand only after dual-run write verification (admin.py's create
endpoint) passes, per the handoff.

Safe to re-run: ALTER TABLE uses IF NOT EXISTS, backfill only touches rows
where club_id IS NULL.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import Club, PairingBlock


def add_column():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE pairing_blocks ADD COLUMN IF NOT EXISTS club_id INTEGER REFERENCES clubs(id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pairing_blocks_club_id ON pairing_blocks(club_id)"
        ))
    print("Added club_id column + index to pairing_blocks (or already present).")


def _manchester_id(session: Session) -> int:
    club = session.exec(select(Club).where(Club.slug == "manchester")).first()
    if club is None:
        raise RuntimeError("Expected Manchester club row (slug='manchester'), not found")
    return club.id


def backfill():
    with Session(engine) as session:
        manchester_id = _manchester_id(session)
        result = session.exec(
            select(PairingBlock).where(PairingBlock.club_id.is_(None))
        ).all()
        for block in result:
            block.club_id = manchester_id
            session.add(block)
        session.commit()
        print(f"Backfilled {len(result)} row(s) to club_id={manchester_id} (Manchester).")


def verify():
    with Session(engine) as session:
        manchester_id = _manchester_id(session)
        null_count = len(session.exec(
            select(PairingBlock).where(PairingBlock.club_id.is_(None))
        ).all())
        total = len(session.exec(select(PairingBlock)).all())
        non_manchester = len(session.exec(
            select(PairingBlock).where(
                PairingBlock.club_id.is_not(None),
                PairingBlock.club_id != manchester_id,
            )
        ).all())

    problems = []
    if null_count != 0:
        problems.append(f"{null_count} row(s) still have club_id IS NULL")
    if non_manchester != 0:
        problems.append(f"{non_manchester} row(s) have club_id set to something other than Manchester ({manchester_id})")

    print(f"pairing_blocks: {total} total row(s), {null_count} NULL club_id, Manchester id={manchester_id}")
    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed.")


def main():
    if "--add-column" in sys.argv:
        add_column()
    if "--backfill" in sys.argv:
        backfill()
    if "--verify" in sys.argv:
        verify()
    if len(sys.argv) == 1:
        print(__doc__)


if __name__ == "__main__":
    main()
