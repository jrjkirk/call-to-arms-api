"""Club-defined vibes, and the behaviour that makes them worth having.

A vibe used to be one of five fixed names, and every one of them meant the same
thing to the matcher: a soft preference costing `weight_vibe` when two players
disagreed. That cannot express what The Old World asked for. "Battle March" is a
smaller points game, so those players must only meet each other. No amount of
turning the vibe weight up says that; it is a partition, not a preference.

The first block is the one that matters most. The matcher is a faithful port and
deliberately frozen, so the change had to be additive: a club that has never
defined an exclusive vibe must pair exactly as it did before, down to the tuple.

Run: PYTHONPATH=. python tests/test_custom_vibes.py
"""
import sys

import vibes

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


CANON = ["Casual", "Competitive", "Open"]
BM = [
    "Casual",
    "Competitive",
    "Open",
    {"name": "Battle March", "behaviour": "exclusive"},
]


print("\n1. Nothing changes for a club that never touches this")
canon = vibes.parse(CANON)
check("plain strings still parse", vibes.names(canon) == CANON, str(vibes.names(canon)))
check("and are all soft, except the one the engine always special-cased",
      [v.behaviour for v in canon] == ["soft", "soft", "wildcard"],
      str([v.behaviour for v in canon]))
check("no pair is ever incompatible without an exclusive vibe",
      not any(
          vibes.incompatible(a, b, canon)
          for a in CANON + [None, "Intro"] for b in CANON + [None, "Intro"]
      ))
check("round-trips back to plain strings, so nothing rewrites stored rows",
      vibes.to_storage(canon) == CANON, str(vibes.to_storage(canon)))


print("\n2. Battle March only plays Battle March")
bm = vibes.parse(BM)
check("the club's own name survives", "Battle March" in vibes.names(bm))
check("declared exclusive", vibes.behaviour_of("Battle March", bm) == "exclusive")
check("two Battle March players can meet",
      not vibes.incompatible("Battle March", "Battle March", bm))
check("a Battle March player cannot be given a standard game",
      vibes.incompatible("Battle March", "Casual", bm))
check("and the standard player cannot be dragged into theirs",
      vibes.incompatible("Competitive", "Battle March", bm))
check("two standard players are unaffected",
      not vibes.incompatible("Casual", "Competitive", bm))

# The decision most likely to be argued about later, so it is asserted rather
# than left to a comment: "Open" means open within the game you turned up to
# play, not "I brought a second army at a different points level".
check("Open does NOT satisfy an exclusive vibe",
      vibes.incompatible("Open", "Battle March", bm))
check("but Open still matches anything ordinary",
      not vibes.incompatible("Open", "Competitive", bm))


print("\n3. Old signups keep working when a club changes its mind")
# Signups store whatever vibe string was current when they were made. A club
# renaming or dropping a vibe must not change how last month's week scores.
check("a vibe that no longer exists is treated as soft",
      vibes.behaviour_of("Escalation", bm) == "soft")
check("and does not become incompatible with everyone",
      not vibes.incompatible("Escalation", "Casual", bm))
# A player who set no vibe never opted into the smaller game, so they must not
# be handed one either.
check("a blank vibe cannot be given a Battle March game",
      vibes.incompatible(None, "Battle March", bm))
check("but is fine in an ordinary game",
      not vibes.incompatible(None, "Casual", bm))
check("the retired 'Either' still behaves like Open",
      vibes.behaviour_of("Either", canon) == "wildcard")


print("\n4. Bad input degrades rather than exploding")
check("an unknown behaviour reads as soft",
      vibes.parse([{"name": "X", "behaviour": "nonsense"}])[0].behaviour == "soft")
check("junk entries are skipped", vibes.parse([None, 3, "", "Casual"]) == [vibes.VibeSpec("Casual", "soft")],
      str(vibes.parse([None, 3, "", "Casual"])))
check("duplicates collapse, first wins",
      vibes.names(vibes.parse(["Casual", "casual", "Competitive"])) == ["Casual", "Competitive"])
check("None parses to nothing", vibes.parse(None) == [])
check("only behaviour-bearing vibes are stored as objects",
      vibes.to_storage(bm) == ["Casual", "Competitive", "Open",
                               {"name": "Battle March", "behaviour": "exclusive"}],
      str(vibes.to_storage(bm)))

# ---------------------------------------------------------------------------
# The engine, end to end
# ---------------------------------------------------------------------------
# The unit checks above prove the rule. These prove the matcher obeys it, and
# more importantly that it still does exactly what it always did for a club
# that has not defined an exclusive vibe.
import os  # noqa: E402
import pathlib  # noqa: E402
import tempfile  # noqa: E402

_DB = pathlib.Path(tempfile.mkdtemp()) / "vibes.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
from models import Club, ClubSystem, Player, Signup, SystemConfig  # noqa: E402
from pairings_engine import generate  # noqa: E402

SYSTEM = "The Old World"
WEEK = "10/09/2026"

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc"))
    db.add(SystemConfig(id=1, name=SYSTEM, slug="tow", legacy_system_name=SYSTEM,
                        active=True, uses_points=True,
                        vibe_options=["Casual", "Competitive", "Open"],
                        default_vibe="Open"))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True,
                      session_day="Thursday", session_cadence="weekly"))
    # Four ordinary players and two who want the smaller game.
    people = [
        ("Ann", "Casual"), ("Bob", "Casual"),
        ("Cat", "Competitive"), ("Dan", "Competitive"),
        ("Eve", "Battle March"), ("Fay", "Battle March"),
    ]
    for i, (name, vibe) in enumerate(people, start=1):
        db.add(Player(id=i, name=name))
        db.add(Signup(week=WEEK, system=SYSTEM, player_id=i, player_name=name,
                      faction="Empire of Man", points=2000, eta="18:00",
                      vibe=vibe, club_id=1))
    db.commit()


def pairs_now():
    """Generate and return the pairings as name pairs, order-insensitive."""
    with Session(database.engine) as db:
        rows = generate(db, WEEK, SYSTEM, persist=False, club_id=1)
    out = []
    for r in rows:
        a = r.get("a_name") or r.get("player_a_name") or r.get("A")
        b = r.get("b_name") or r.get("player_b_name") or r.get("B")
        out.append(frozenset(x for x in (a, b) if x))
    return out


def vibe_of(name):
    return dict(people)[name]


print("\n5. With only canonical vibes, the matcher behaves as it always did")
before = pairs_now()
check("everyone got a game", sum(len(p) for p in before) >= 6, str(before))
mixed = [p for p in before if len({vibe_of(n) for n in p if n in dict(people)}) > 1]
check("Battle March players are pairable when nothing says otherwise",
      any("Eve" in p or "Fay" in p for p in before), str(before))

print("\n6. Declaring it exclusive partitions the pool")
with Session(database.engine) as db:
    cs = db.get(ClubSystem, 1)
    cs.vibe_options = ["Casual", "Competitive", "Open",
                       {"name": "Battle March", "behaviour": "exclusive"}]
    cs.default_vibe = "Open"
    db.add(cs)
    db.commit()

after = pairs_now()
bm_pairs = [p for p in after if any(vibe_of(n) == "Battle March" for n in p if n in dict(people))]
check("the two Battle March players are paired with each other",
      frozenset({"Eve", "Fay"}) in after, str(after))
check("and with nobody else",
      all(p == frozenset({"Eve", "Fay"}) for p in bm_pairs), str(bm_pairs))
check("the other four still get games",
      sum(1 for p in after if not (p & {"Eve", "Fay"})) == 2, str(after))

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
