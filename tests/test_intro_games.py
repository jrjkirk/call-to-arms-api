"""Intro games: a newcomer asking to be taught, and who they end up opposite.

Two controls make an intro game possible. A player asks for one by picking the
"Intro" vibe; another offers to teach one by ticking "I can lead an intro game"
(can_demo). Whether the matcher does anything about it used to be a THIRD flag,
has_intro_prepass, which could silently disagree with both.

The rule now IS the whole feature: if a legal teacher is free, the seeker gets
one; if not, they are matched on everything else exactly as anyone would be.
That is a tier in _pair_dist, below blocks and above every soft factor, so
there is nothing to configure and no middle setting to get wrong.

The pre-pass it replaced ran before the matcher, paired each seeker with the
nearest teacher and removed both from the pool. It scored (vibe, experience,
points) distance and nothing else, so `blocks` and `last_opp_pairs` were not in
scope. Block 2 is the bug that came from that.

Run: PYTHONPATH=. python tests/test_intro_games.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "intro.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, Pairing, PairingBlock, Player, Signup, SystemConfig,
)
from pairings_engine import generate  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SYS = "The Old World"
WEEK = "08/10/2026"

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="C", slug="c"))
    db.add(SystemConfig(id=1, name=SYS, slug="tow", legacy_system_name=SYS, active=True,
                        uses_points=True, default_points=2000, max_points=3000,
                        allows_demo=True,
                        vibe_options=["Casual", "Intro"], default_vibe="Casual"))
    db.add(ClubSystem(club_id=1, system_id=1, enabled=True,
                      session_day="Thursday", session_cadence="weekly"))
    db.commit()


def setup(people, blocks=()):
    """people: (name, vibe, can_demo). Everything else is identical between
    them, so the intro weight is the only thing that can decide a pairing."""
    with Session(database.engine) as db:
        for row in db.exec(select(Signup)).all():
            db.delete(row)
        for row in db.exec(select(Pairing)).all():
            db.delete(row)
        for row in db.exec(select(PairingBlock)).all():
            db.delete(row)
        for pid, (name, vibe, demo) in enumerate(people, start=1):
            if db.get(Player, pid) is None:
                db.add(Player(id=pid, name=name, club_id=1))
            db.add(Signup(week=WEEK, system=SYS, player_id=pid, player_name=name,
                          faction="Skaven", points=2000, eta="18:00",
                          vibe=vibe, can_demo=demo, club_id=1))
        for a, b in blocks:
            lo, hi = sorted([a, b])
            db.add(PairingBlock(player_a_id=lo, player_b_id=hi, club_id=1))
        db.commit()


def pairs():
    with Session(database.engine) as db:
        rows = generate(db, WEEK, SYS, persist=False, club_id=1)
    return [frozenset(x for x in (r.get("a_name"), r.get("b_name")) if x) for r in rows]


print("\n1. A newcomer is put opposite someone who offered to teach")
setup([("Ann", "Intro", False), ("Bob", "Casual", True),
       ("Cat", "Casual", False), ("Dan", "Casual", False)])
p = pairs()
check("Ann gets Bob, the only teacher in the room",
      frozenset({"Ann", "Bob"}) in p, str(p))
check("and everyone is still paired", sum(len(x) for x in p) == 4, str(p))


print("\n2. It no longer walks through an admin block")
# THE BUG. The pre-pass scored (vibe, experience, points) distance only, so
# with two identical teachers it took the first — even when an admin had
# blocked that exact pair, and an equally close unblocked teacher was free.
setup([("Ann", "Intro", False), ("Bob", "Casual", True),
       ("Cat", "Casual", True), ("Dan", "Casual", False)],
      blocks=[(1, 2)])
p = pairs()
check("Ann is NOT paired with the teacher she is blocked from",
      frozenset({"Ann", "Bob"}) not in p, str(p))
check("she is paired with the other teacher instead",
      frozenset({"Ann", "Cat"}) in p, str(p))
check("and nobody lost a game over it", sum(len(x) for x in p) == 4, str(p))


print("\n3. Two newcomers are the case it exists to prevent")
setup([("Ann", "Intro", False), ("Eve", "Intro", False),
       ("Bob", "Casual", True), ("Cat", "Casual", True)])
p = pairs()
check("they are split up, one teacher each",
      frozenset({"Ann", "Eve"}) not in p, str(p))
check("and both are opposite a teacher",
      all(any(n in x for n in ("Bob", "Cat")) for x in p if "Ann" in x or "Eve" in x),
      str(p))


print("\n4. A teacher who is also a newcomer counts as a teacher")
# can_demo is checked on its own rather than excluding seekers from the teacher
# side, so someone new to this system who can still show the rules satisfies it.
setup([("Ann", "Intro", False), ("Eve", "Intro", True)])
p = pairs()
check("the two of them can be paired", frozenset({"Ann", "Eve"}) in p, str(p))


print("\n5. It outranks every soft factor, and no hard one")
# The tier sits below format, last opponent and blocks, and above the entire
# weighted score, so a mirror match or a vibe mismatch never costs a newcomer
# their teacher...
setup([("Ann", "Intro", False), ("Bob", "Casual", True), ("Cat", "Casual", False)])
p = pairs()
check("a teacher is taken over a same-faction, same-vibe alternative",
      frozenset({"Ann", "Bob"}) in p, str(p))

# ...but with the only teacher blocked, the block still wins. That is the whole
# difference from the pre-pass, which had no idea blocks existed.
setup([("Ann", "Intro", False), ("Bob", "Casual", True), ("Cat", "Casual", False),
       ("Dan", "Casual", False)],
      blocks=[(1, 2)])
p = pairs()
check("a block beats the intro rule", frozenset({"Ann", "Bob"}) not in p, str(p))
check("and Ann still gets a game, just not a taught one",
      any("Ann" in x and len(x) == 2 for x in p), str(p))


print("\n6. With no teacher in the room, nothing changes for anyone")
# The other half of the rule, and what makes it safe to have no switch at all:
# a seeker with nobody to learn from is matched on the ordinary factors, not
# held back and not given a BYE.
setup([("Ann", "Intro", False), ("Bob", "Casual", False),
       ("Cat", "Casual", False), ("Dan", "Casual", False)])
none_free = pairs()
check("everyone is paired", sum(len(x) for x in none_free) == 4, str(none_free))
check("Ann has a real opponent",
      any("Ann" in x and len(x) == 2 for x in none_free), str(none_free))


print("\n7. A system with no intro seekers is untouched")
# Nobody can be a seeker, so the tier is 0 for every pair and the tuple sorts
# exactly as it did before intro matching existed.
setup([("Ann", "Casual", False), ("Bob", "Casual", True),
       ("Cat", "Casual", False), ("Dan", "Casual", False)])
plain = pairs()
check("everyone is paired on the ordinary factors",
      sum(len(x) for x in plain) == 4, str(plain))
check("and offering to teach costs the teacher nothing",
      any("Bob" in x and len(x) == 2 for x in plain), str(plain))


print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
