"""Pairing across faction categories: Good vs Evil, Axis vs Allies.

The matcher has always known whether two players brought the SAME faction (the
mirror flag) and nothing else about what the two armies are. On a system with
categories that left the obvious thing unsaid: Rohan vs Gondor scored exactly
the same as Rohan vs Mordor.

The first block is the one that matters. Every flat-list system — five of the
six in production — must pair byte-identically to before, because the engine is
a faithful port and deliberately frozen. The category weight only exists for a
system that defines categories; everywhere else it must be provably inert.

Run: PYTHONPATH=. python tests/test_faction_categories.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "categories.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, PairingConfig, Player, Signup, SystemConfig,
)
from pairings_engine import _faction_group_index, generate  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


FLAT = "Kill Team"          # no categories, in code or in the DB
GROUPED = "Bolt Action"     # categories authored in the platform admin UI
WEEK = "17/09/2026"

AXIS = ["Germany", "Italy"]
ALLIES = ["Soviet Union", "United States"]

SQLModel.metadata.create_all(database.engine)

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc"))
    db.add(SystemConfig(id=1, name=FLAT, slug="kt", legacy_system_name=FLAT,
                        active=True, uses_points=False,
                        vibe_options=["Open"], default_vibe="Open"))
    db.add(SystemConfig(
        id=2, name=GROUPED, slug="ba", legacy_system_name=GROUPED, active=True,
        uses_points=False, vibe_options=["Open"], default_vibe="Open",
        faction_list=AXIS + ALLIES,
        faction_groups=[{"label": "Axis", "factions": AXIS},
                        {"label": "Allies", "factions": ALLIES}],
    ))
    for sid in (1, 2):
        db.add(ClubSystem(id=sid, club_id=1, system_id=sid, enabled=True,
                          session_day="Thursday", session_cadence="weekly"))
    db.commit()


def signup(system, pid, name, faction):
    with Session(database.engine) as db:
        db.add(Player(id=pid, name=name, club_id=1)) if not db.get(Player, pid) else None
        db.add(Signup(week=WEEK, system=system, player_id=pid, player_name=name,
                      faction=faction, eta="18:00", vibe="Open", club_id=1))
        db.commit()


def clear_signups(system):
    with Session(database.engine) as db:
        for s in db.exec(select(Signup).where(Signup.system == system)).all():
            db.delete(s)
        db.commit()


def set_weight(system_id, value):
    with Session(database.engine) as db:
        cfg = db.exec(select(PairingConfig).where(
            PairingConfig.club_id == 1, PairingConfig.system_id == system_id)).first()
        if cfg is None:
            cfg = PairingConfig(club_id=1, system_id=system_id)
        cfg.weight_faction_group = value
        db.add(cfg)
        db.commit()


def pairs(system):
    with Session(database.engine) as db:
        rows = generate(db, WEEK, system, persist=False, club_id=1)
    return [frozenset(x for x in (r.get("a_name"), r.get("b_name")) if x) for r in rows]


def cross_count(result, side_a, side_b):
    """How many pairings put one side against the other."""
    n = 0
    for p in result:
        names = list(p)
        if len(names) != 2:
            continue
        if (names[0] in side_a and names[1] in side_b) or \
           (names[1] in side_a and names[0] in side_b):
            n += 1
    return n


print("\n1. A flat-list system is completely unaffected")
# Kill Team has no categories in code and none in the DB, so the index is empty
# and the flag is 0 for every pair no matter what the weight is set to.
with Session(database.engine) as db:
    check("its category index is empty", _faction_group_index(db.get(SystemConfig, 1)) == {})

for pid, name, fac in [(1, "Ann", "Kommandos"), (2, "Bob", "Kommandos"),
                       (3, "Cat", "Pathfinders"), (4, "Dan", "Pathfinders")]:
    signup(FLAT, pid, name, fac)

set_weight(1, 0.0)
flat_off = pairs(FLAT)
set_weight(1, 10.0)
flat_max = pairs(FLAT)
check("pairings are identical at weight 0 and weight 10",
      flat_off == flat_max, f"{flat_off} vs {flat_max}")
check("and everyone still gets a game",
      sum(len(p) for p in flat_off) == 4, str(flat_off))


print("\n2. The index is built from the authored categories")
with Session(database.engine) as db:
    idx = _faction_group_index(db.get(SystemConfig, 2))
check("every faction is placed", len(idx) == 4, str(idx))
check("lookups are case-insensitive, as the stored signup values are not normalised",
      idx.get("germany") == "Axis" and idx.get("soviet union") == "Allies", str(idx))


print("\n3. Two Axis and two Allies get paired across the line")
# Left alone, the greedy matcher pairs candidate 1 with candidate 2 — which
# here is Germany against Italy, an Axis mirror-in-spirit. The weight is the
# only thing that can separate them, because every other factor is identical.
clear_signups(GROUPED)
for pid, name, fac in [(5, "Eve", "Germany"), (6, "Fay", "Italy"),
                       (7, "Gus", "Soviet Union"), (8, "Hal", "United States")]:
    signup(GROUPED, pid, name, fac)

set_weight(2, 0.0)
off = pairs(GROUPED)
set_weight(2, 1.0)
on = pairs(GROUPED)

axis_players, allied_players = {"Eve", "Fay"}, {"Gus", "Hal"}
check("with the weight off, both games are same-category",
      cross_count(off, axis_players, allied_players) == 0, str(off))
check("with it on, both games are Axis against Allies",
      cross_count(on, axis_players, allied_players) == 2, str(on))
check("and nobody lost a game to get there",
      sum(len(p) for p in on) == 4, str(on))


print("\n4. It is a nudge, not a bar")
# Three Axis to one Allied player: one same-category game is unavoidable. The
# category weight must never leave someone without an opponent to avoid it —
# that is the difference between a weight and the hard block/last-opponent
# filters, and it is the whole reason this went in the weighted score.
clear_signups(GROUPED)
for pid, name, fac in [(5, "Eve", "Germany"), (6, "Fay", "Italy"),
                       (9, "Ivy", "Italy"), (8, "Hal", "United States")]:
    signup(GROUPED, pid, name, fac)
set_weight(2, 10.0)
lopsided = pairs(GROUPED)
check("everyone is still paired even at the maximum weight",
      sum(len(p) for p in lopsided) == 4, str(lopsided))
check("and the one Allied player got the cross-category game",
      cross_count(lopsided, {"Eve", "Fay", "Ivy"}, {"Hal"}) == 1, str(lopsided))


print("\n5. An unknown or missing faction has no opinion")
# A player who has not picked yet, or one carrying a historical faction name
# no longer in the list, must not be pushed around by a category nobody can
# see. Both must land in a category-free state, not in a shared "None" bucket
# that would make them repel each other.
clear_signups(GROUPED)
for pid, name, fac in [(5, "Eve", None), (6, "Fay", "Kingdom of Italy"),
                       (7, "Gus", "Soviet Union"), (8, "Hal", "United States")]:
    signup(GROUPED, pid, name, fac)
set_weight(2, 10.0)
unknowns = pairs(GROUPED)
check("everyone is paired", sum(len(p) for p in unknowns) == 4, str(unknowns))
check("the two Allied players are free to meet each other, since the other two "
      "carry no category at all",
      frozenset({"Gus", "Hal"}) in unknowns or cross_count(unknowns, {"Eve", "Fay"}, {"Gus", "Hal"}) == 2,
      str(unknowns))


print("\n6. The default is on, and reaches an untouched club")
# A club that has never opened the sliders has no PairingConfig row at all, so
# the model default is what actually decides this for almost everyone.
with Session(database.engine) as db:
    for cfg in db.exec(select(PairingConfig).where(PairingConfig.system_id == 2)).all():
        db.delete(cfg)
    db.commit()
clear_signups(GROUPED)
for pid, name, fac in [(5, "Eve", "Germany"), (6, "Fay", "Italy"),
                       (7, "Gus", "Soviet Union"), (8, "Hal", "United States")]:
    signup(GROUPED, pid, name, fac)
default_on = pairs(GROUPED)
check("with no saved config, the games still come out across the categories",
      cross_count(default_on, {"Eve", "Fay"}, {"Gus", "Hal"}) == 2, str(default_on))
check("and it sits below vibe, so it cannot outrank what kind of game "
      "someone asked for",
      PairingConfig().weight_faction_group < PairingConfig().weight_vibe,
      f"{PairingConfig().weight_faction_group} vs {PairingConfig().weight_vibe}")


print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
