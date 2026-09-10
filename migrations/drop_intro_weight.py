"""Drop `pairing_configs.weight_intro`, added and retired the same day.

It existed for a few hours as a 0-10 slider for "how hard should the matcher
try to give a newcomer a teacher". That was the wrong shape for the question.
There is no sensible middle setting: the answer is yes or no, so every club
would have left it at the maximum or dragged it to zero, and the ninety-eight
positions in between were a choice nobody could act on.

Intro matching is a TIER in pairings_engine._pair_dist now — below format, last
opponent and blocks, above every soft factor. A player who asked to be taught
takes a teacher whenever a legal one is free, and falls through to the ordinary
factors when none is. Nothing to configure, so nothing to store.

## Run this AFTER the code that stopped reading the column is deployed

The opposite order from an additive migration. Dropping a column the running
image still selects breaks every read of that table, so:

    1. deploy the API without weight_intro   <- already done if you are here
    2. run this

Safe to re-run; DROP COLUMN IF EXISTS is a no-op the second time.

    PYTHONPATH=. python migrations/drop_intro_weight.py
    PYTHONPATH=. python migrations/drop_intro_weight.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def drop_column():
    with Session(engine) as session:
        session.exec(text("ALTER TABLE pairing_configs DROP COLUMN IF EXISTS weight_intro"))
        session.commit()
    print("Dropped pairing_configs.weight_intro (or it was already gone).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        if session.exec(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'pairing_configs' AND column_name = 'weight_intro'"
        )).first() is not None:
            problems.append("weight_intro is still present")
        # The other weights must be untouched — this is a drop, and a drop is
        # the one migration shape that can take something with it.
        for col in ("weight_mirror", "weight_faction_group", "weight_rematch",
                    "weight_vibe", "weight_experience", "weight_eta",
                    "weight_scenario", "weight_points"):
            if session.exec(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'pairing_configs' AND column_name = :c"
            ), params={"c": col}).first() is None:
                problems.append(f"{col} went missing")
        rows = session.exec(text("SELECT count(*) FROM pairing_configs")).first()
        print(f"  pairing configs still readable: {rows[0]}")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        drop_column()
    issues = verify()
    if issues:
        print("VERIFY FAILED:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("Verified: weight_intro is gone and every other weight survived.")
