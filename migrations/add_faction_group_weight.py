"""Add `weight_faction_group` to `pairing_configs`.

The matcher has always known about mirror matches — two players bringing the
same faction — and nothing else about what the two armies are. On a system with
faction categories that leaves the obvious thing unsaid: in Middle Earth,
Rohan vs Gondor scored exactly the same as Rohan vs Mordor, and in Bolt Action
Germany vs Italy scores the same as Germany vs Soviet Union. Both are Good vs
Good / Axis vs Axis, which is the pairing the club would rather not have.

    weight_faction_group  double precision NOT NULL DEFAULT 1.0

## Why NOT NULL DEFAULT 1.0 and not 0

Postgres fills existing rows with the default, so every club that has already
tuned its sliders gets this factor switched on at 1.0 rather than off. That is
deliberate: it is the behaviour being asked for, it is visible on the same
slider row as every other weight, and a club that dislikes it drags it to 0.
Defaulting to 0 would have shipped a feature nobody could tell was there.

1.0 sits below vibe (1.5) and above experience (0.8). A same-category pairing
is a thematic disappointment; a casual player against a competitive one is a
bad evening. The nudge must not outrank what kind of game someone asked for.

## Systems this actually changes

Only ones with authored faction categories: Middle Earth (Good/Evil) today,
plus any system authored with categories from the platform admin UI. Every
flat-list system computes a flag of 0 for every pair, so the weight is inert
and their pairings are byte-identical to before.

One-off script, not a long-lived migration tool (see CLAUDE.md / models.py
docstring). Additive + idempotent, so it is safe to re-run.

    PYTHONPATH=. python migrations/add_faction_group_weight.py
    PYTHONPATH=. python migrations/add_faction_group_weight.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_column():
    with Session(engine) as session:
        session.exec(text(
            "ALTER TABLE pairing_configs ADD COLUMN IF NOT EXISTS "
            "weight_faction_group DOUBLE PRECISION NOT NULL DEFAULT 1.0"
        ))
        session.commit()
    print("Added pairing_configs.weight_faction_group (or it was already there).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        row = session.exec(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'pairing_configs' AND column_name = 'weight_faction_group'"
        )).first()
        if row is None:
            problems.append("column weight_faction_group is missing")
        else:
            data_type, is_nullable, default = row
            if data_type != "double precision":
                problems.append(f"expected double precision, got {data_type}")
            # NOT NULL matters: the engine multiplies by this value directly,
            # and a NULL would raise rather than degrade.
            if is_nullable != "NO":
                problems.append(f"expected NOT NULL, got is_nullable={is_nullable}")
            if default is None or "1.0" not in str(default):
                problems.append(f"expected default 1.0, got {default!r}")

        rows = session.exec(text(
            "SELECT count(*), count(*) FILTER (WHERE weight_faction_group = 1.0) "
            "FROM pairing_configs"
        )).first()
        if rows:
            print(f"  saved pairing configs: {rows[0]}, of which at the 1.0 default: {rows[1]}")
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
    print("Verified: pairing_configs.weight_faction_group is NOT NULL double precision defaulting to 1.0.")
