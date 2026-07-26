"""Drop the redundant `location` column from `call_outs`.

A call-out is always at whichever club the player is at (it's posted and shown
per-club and only notified to that club's Discord), so an explicit location
field was redundant — removed from the form and the model. This drops the
now-unused column.

One-off script (this repo doesn't manage migrations — see CLAUDE.md). Uses
IF EXISTS so it's safe to re-run and a no-op once already dropped.

Run (from repo root):
    PYTHONPATH=. python migrations/drop_location_from_call_outs.py
"""
from sqlalchemy import text
from sqlmodel import Session

from database import engine


def main() -> None:
    with Session(engine) as session:
        session.exec(text("ALTER TABLE call_outs DROP COLUMN IF EXISTS location"))
        session.commit()
        still_there = session.exec(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'call_outs' AND column_name = 'location'"
        )).first()
    if still_there:
        raise SystemExit("FAILED — call_outs.location still exists after drop.")
    print("OK — call_outs.location dropped (or already absent).")


if __name__ == "__main__":
    main()
