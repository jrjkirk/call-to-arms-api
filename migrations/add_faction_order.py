"""Faction order (2026-10-21): systems.faction_order, and sort five lists.

Adds `faction_order` ("alphabetical" | "custom", NOT NULL DEFAULT 'custom'),
then sets the systems whose lists are meant to be alphabetical to it and sorts
their stored lists to match. The rest stay "custom" in the order they already
have: Horus Heresy runs in Legion number order, The Old World and StarCraft in
the game's own.

Only the ORDER of each list changes, never a name, so no signup is orphaned.
The script checks that before writing.

RUN BEFORE DEPLOYING the faction-order code: models.SystemConfig gains the
column, and every query on `systems` would fail without it. Old code never
reads it, so running it early is harmless (old code serves the sorted list as
stored, which is the point).

Run from a laptop whose .env reaches production (it imports systems/, which
the live image predates):

    PYTHONPATH=. python migrations/add_faction_order.py              # dry run
    PYTHONPATH=. python migrations/add_faction_order.py --apply
    PYTHONPATH=. python migrations/add_faction_order.py --verify-only

One transaction; idempotent.
"""
import sys

from sqlalchemy import text

from database import engine
from systems import ordered_factions

ALPHABETICAL = ["Kill Team", "Warhammer 40,000", "Age of Sigmar", "Bolt Action", "Middle Earth"]

ADD_COLUMN = (
    "ALTER TABLE systems ADD COLUMN IF NOT EXISTS faction_order VARCHAR NOT NULL DEFAULT 'custom'"
)


def _rows(conn):
    return conn.execute(text(
        "SELECT id, legacy_system_name, faction_list, faction_groups FROM systems"
    )).mappings().all()


def _plan(conn) -> list[tuple]:
    plan = []
    for r in _rows(conn):
        if r["legacy_system_name"] not in ALPHABETICAL:
            continue
        factions, groups = list(r["faction_list"] or []), r["faction_groups"]
        new_f, new_g = ordered_factions(factions, groups, "alphabetical")
        same_names = sorted(new_f) == sorted(factions) and (
            [sorted(g["factions"]) for g in new_g or []] == [sorted(g["factions"]) for g in groups or []]
        )
        if not same_names:
            raise RuntimeError(f"{r['legacy_system_name']}: sorting changed the names, refusing.")
        plan.append((r["id"], r["legacy_system_name"], factions, new_f, groups, new_g))
    missing = set(ALPHABETICAL) - {p[1] for p in plan}
    if missing:
        raise RuntimeError(f"No system named {sorted(missing)}.")
    return plan


def verify(conn) -> list[str]:
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'systems'"
    )).all()}
    if "faction_order" not in cols:
        return ["systems.faction_order does not exist"]
    problems = []
    for r in conn.execute(text(
        "SELECT legacy_system_name, faction_order, faction_list, faction_groups FROM systems"
    )).mappings().all():
        want = "alphabetical" if r["legacy_system_name"] in ALPHABETICAL else "custom"
        if r["faction_order"] != want:
            problems.append(f"{r['legacy_system_name']}: faction_order {r['faction_order']!r}, expected {want!r}")
        if want == "alphabetical":
            f, g = ordered_factions(list(r["faction_list"] or []), r["faction_groups"], "alphabetical")
            if f != list(r["faction_list"] or []) or g != r["faction_groups"]:
                problems.append(f"{r['legacy_system_name']}: list is not sorted")
    return problems


def main() -> None:
    import json

    if "--verify-only" in sys.argv:
        with engine.connect() as conn:
            problems = verify(conn)
    elif "--apply" in sys.argv:
        with engine.begin() as conn:
            conn.execute(text(ADD_COLUMN))
            for sid, name, _old, new_f, _old_g, new_g in _plan(conn):
                conn.execute(
                    text("UPDATE systems SET faction_order = 'alphabetical', "
                         "faction_list = CAST(:f AS JSON), faction_groups = CAST(:g AS JSON) "
                         "WHERE id = :id"),
                    {"f": json.dumps(new_f), "g": json.dumps(new_g) if new_g is not None else None,
                     "id": sid},
                )
            problems = verify(conn)
            if problems:
                raise RuntimeError(f"verification failed, rolling back: {problems}")
        print("APPLIED.")
    else:
        with engine.connect() as conn:
            plan = _plan(conn)
        print("DRY RUN, nothing written. --apply would:")
        print(f"  {ADD_COLUMN}")
        for _sid, name, old, new_f, old_g, new_g in plan:
            moved = [f for i, f in enumerate(new_f) if i >= len(old) or old[i] != f]
            print(f"  {name}: set alphabetical; {len(moved)} of {len(old)} factions change position"
                  + (f" (e.g. {', '.join(moved[:4])})" if moved else ""))
        return

    if problems:
        print(f"VERIFICATION FAILED: {problems}")
        sys.exit(1)
    print("Verification passed.")


if __name__ == "__main__":
    main()
