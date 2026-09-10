"""Add `weight_intro` to `pairing_configs`, replacing the intro pre-pass.

The intro pre-pass ran BEFORE the matcher: anyone whose vibe was "Intro" was
paired with the nearest player who had ticked "I can lead an intro game", both
were removed from the pool, and the real matcher never saw them.

It scored on (vibe, experience, points) distance and nothing else. `blocks` and
`last_opp_pairs` were not even in scope in that loop, so it could — and did —
pair two players an admin had explicitly blocked, while an equally close,
unblocked teacher sat free. Same omission meant it ignored last week's opponent
and exclusive vibes, so a 1000pt Battle March player who offered to teach could
be pulled into a 2000pt intro game.

As a weight it goes through _pair_dist like every other factor and inherits all
of those filters for free.

    weight_intro  double precision NOT NULL DEFAULT 8.0

## Why 8.0, above even weight_mirror

Every other weight is about a preference. This one is about a person: a
newcomer opposite another newcomer has nobody to show them the game, which is
the outcome the feature exists to prevent. 8.0 beats mirror (5.0) and a recent
rematch (3.0 x 2), so the matcher would rather give a newcomer a teacher they
played recently than a stranger who cannot teach.

It does NOT beat a block or last week's opponent. Those are separate tiers
above the weighted score entirely, which is the whole point of moving this out
of the pre-pass.

## has_intro_prepass is now unread

Nothing sets or clears it in this migration and the column stays on both
tables. Intro matching is now governed by what the signup form actually offers
— the Intro vibe and the "I can lead an intro game" checkbox — so the flag
could no longer disagree with them. Every system offering the Intro vibe on
2026-09-10 (Kill Team, Horus Heresy, The Old World, Age of Sigmar, Bolt Action)
already had the flag true, so retiring it changed nothing for anybody.

One-off script, not a long-lived migration tool (see CLAUDE.md / models.py
docstring). Additive + idempotent, so it is safe to re-run.

    PYTHONPATH=. python migrations/add_intro_weight.py
    PYTHONPATH=. python migrations/add_intro_weight.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_column():
    with Session(engine) as session:
        session.exec(text(
            "ALTER TABLE pairing_configs ADD COLUMN IF NOT EXISTS "
            "weight_intro DOUBLE PRECISION NOT NULL DEFAULT 8.0"
        ))
        session.commit()
    print("Added pairing_configs.weight_intro (or it was already there).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        row = session.exec(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'pairing_configs' AND column_name = 'weight_intro'"
        )).first()
        if row is None:
            problems.append("column weight_intro is missing")
        else:
            data_type, is_nullable, default = row
            if data_type != "double precision":
                problems.append(f"expected double precision, got {data_type}")
            if is_nullable != "NO":
                problems.append(f"expected NOT NULL, got is_nullable={is_nullable}")
            if default is None or "8" not in str(default):
                problems.append(f"expected default 8.0, got {default!r}")

        rows = session.exec(text(
            "SELECT count(*), count(*) FILTER (WHERE weight_intro = 8.0) FROM pairing_configs"
        )).first()
        if rows:
            print(f"  saved pairing configs: {rows[0]}, of which at the 8.0 default: {rows[1]}")

        # Which systems intro matching now applies to, so the deploy can be
        # eyeballed against the list in this docstring.
        offering = session.exec(text(
            "SELECT name FROM systems WHERE allows_demo = true "
            "AND vibe_options::text ILIKE '%intro%' ORDER BY name"
        )).all()
        print(f"  systems offering intro games: {[o[0] for o in offering]}")
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
    print("Verified: pairing_configs.weight_intro is NOT NULL double precision defaulting to 8.0.")
