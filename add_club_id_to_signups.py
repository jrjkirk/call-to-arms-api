"""Phase 1, table 6 of 10 (signups): expand/backfill/dual-run steps for
adding club_id to signups.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). `signups` already
exists as a live table with real rows (this is the highest-traffic table
touched so far — the live weekly Call to Arms signup flow), so SQLModel's
`create_all()` won't alter it; this script runs the ALTER TABLE by hand,
then backfills existing rows to Manchester (the only club today).

Run manually, in order:

    python add_club_id_to_signups.py --add-column   # expand: add nullable column + index
    python add_club_id_to_signups.py --backfill      # backfill existing rows to Manchester
    python add_club_id_to_signups.py --verify        # check: no NULLs, column present

The contract step (SET NOT NULL) is deliberately not run automatically here
— use --contract by hand only after dual-run write verification (all three
create sites: submit_signup, submit_prearranged, admin_signup_create)
passes, per the handoff.

Safe to re-run: ALTER TABLE uses IF NOT EXISTS, backfill only touches rows
where club_id IS NULL.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import Club, Signup


def add_column():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE signups ADD COLUMN IF NOT EXISTS club_id INTEGER REFERENCES clubs(id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_signups_club_id ON signups(club_id)"
        ))
    print("Added club_id column + index to signups (or already present).")


def _manchester_id(session: Session) -> int:
    club = session.exec(select(Club).where(Club.slug == "manchester")).first()
    if club is None:
        raise RuntimeError("Expected Manchester club row (slug='manchester'), not found")
    return club.id


def backfill():
    with Session(engine) as session:
        manchester_id = _manchester_id(session)
        total_before = len(session.exec(select(Signup)).all())
        result = session.exec(
            select(Signup).where(Signup.club_id.is_(None))
        ).all()
        for su in result:
            su.club_id = manchester_id
            session.add(su)
        session.commit()
        print(f"signups before backfill: {total_before} total row(s).")
        print(f"Backfilled {len(result)} row(s) to club_id={manchester_id} (Manchester).")


def verify():
    with Session(engine) as session:
        manchester_id = _manchester_id(session)
        null_count = len(session.exec(
            select(Signup).where(Signup.club_id.is_(None))
        ).all())
        total = len(session.exec(select(Signup)).all())
        manchester_count = len(session.exec(
            select(Signup).where(Signup.club_id == manchester_id)
        ).all())
        non_manchester = len(session.exec(
            select(Signup).where(
                Signup.club_id.is_not(None),
                Signup.club_id != manchester_id,
            )
        ).all())

    problems = []
    if null_count != 0:
        problems.append(f"{null_count} row(s) still have club_id IS NULL")
    if non_manchester != 0:
        problems.append(f"{non_manchester} row(s) have club_id set to something other than Manchester ({manchester_id})")
    if manchester_count != total:
        problems.append(f"only {manchester_count} of {total} row(s) have club_id = Manchester ({manchester_id})")

    print(f"signups: {total} total row(s), {null_count} NULL club_id, {manchester_count} Manchester, Manchester id={manchester_id}")
    if problems:
        print("VERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("Verification passed.")


def contract():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE signups ALTER COLUMN club_id SET NOT NULL"))
    print("signups.club_id is now NOT NULL.")


def main():
    if "--add-column" in sys.argv:
        add_column()
    if "--backfill" in sys.argv:
        backfill()
    if "--verify" in sys.argv:
        verify()
    if "--contract" in sys.argv:
        contract()
    if len(sys.argv) == 1:
        print(__doc__)


if __name__ == "__main__":
    main()
