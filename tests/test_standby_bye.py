"""Standby: a player who ticks "happy to be a standby" takes the BYE.

From 2026-07-05 until 2026-09-15 the candidate sort put standby players at the
FRONT of the greedy queue, so they always got first pick of partners and were
never the one left over. On 15/09 The Old World paired all five volunteers and
gave the BYE to someone who had not offered.

Sorting them to the back is not enough on its own: greedy matching lets an
earlier player pick a volunteer as their best partner, leaving a non-volunteer
over. So the BYE is chosen explicitly before matching.

Run: PYTHONPATH=. python tests/test_standby_bye.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "standby.db"
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
WEEK = "16/09/2026"
LAST_WEEK = "09/09/2026"

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="C", slug="c"))
    db.add(SystemConfig(id=1, name=SYS, slug="tow", legacy_system_name=SYS, active=True,
                        uses_points=True, default_points=2000, max_points=3000,
                        uses_standby=True, allows_demo=True,
                        vibe_options=["Casual", "Open", "Competitive", "Intro"],
                        default_vibe="Casual"))
    db.add(ClubSystem(club_id=1, system_id=1, enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    db.commit()


def setup(people, blocks=(), last_week_byes=()):
    """people: (name, vibe, standby). Signed up in list order."""
    t0 = datetime(2026, 9, 10, 12, 0)
    with Session(database.engine) as db:
        for model in (Pairing, Signup, PairingBlock):
            for row in db.exec(select(model)).all():
                db.delete(row)
        db.commit()
        for pid, (name, vibe, standby) in enumerate(people, start=1):
            if db.get(Player, pid) is None:
                db.add(Player(id=pid, name=name, club_id=1))
            db.add(Signup(week=WEEK, system=SYS, player_id=pid, player_name=name,
                          faction="Skaven", points=2000, eta="18:00", vibe=vibe,
                          standby_ok=standby, club_id=1,
                          created_at=t0 + timedelta(minutes=pid)))
        for a, b in blocks:
            lo, hi = sorted([a, b])
            db.add(PairingBlock(player_a_id=lo, player_b_id=hi, club_id=1))
        for pid in last_week_byes:
            su = Signup(week=LAST_WEEK, system=SYS, player_id=pid,
                        player_name=people[pid - 1][0], club_id=1)
            db.add(su)
            db.flush()
            db.add(Pairing(week=LAST_WEEK, system=SYS, a_signup_id=su.id, b_signup_id=None,
                           status="published", club_id=1))
        db.commit()


def run():
    with Session(database.engine) as db:
        rows = generate(db, WEEK, SYS, persist=False, club_id=1)
    byes = [r["a_name"] for r in rows if r["b_signup_id"] is None]
    games = [(r["a_name"], r["b_name"]) for r in rows if r["b_signup_id"] is not None]
    return byes, games


# The shape of the real 16/09 week: 19 players, 5 volunteers of mixed vibes,
# two intro seekers with no teacher.
REAL = [
    ("Sam Page", "Casual", False), ("Allan Prossor", "Casual", False),
    ("Damian Zukowski", "Competitive", True), ("Kieren Elliott", "Open", True),
    ("Joel Kirk", "Open", False), ("Stuart D", "Casual", False),
    ("Jonathan Lewis", "Intro", False), ("Rory", "Intro", False),
    ("Shaun Warne", "Casual", False), ("Conor Crook", "Casual", False),
    ("Ade R", "Open", False), ("Mark Williams", "Casual", False),
    ("Mitch", "Competitive", False), ("Jason M", "Casual", False),
    ("Antonio Sanchez-Carrasco", "Open", False), ("Sam Hamer", "Casual", True),
    ("Ben Ramsay", "Casual", True), ("Steve Green", "Open", True),
    ("Matt Cook", "Open", False),
]
VOLUNTEERS = {n for n, _, s in REAL if s}

print("1. The real 16/09 week: the BYE goes to a volunteer")
setup(REAL)
byes, games = run()
check("exactly one BYE", len(byes) == 1, byes)
check("the BYE is a standby volunteer", byes and byes[0] in VOLUNTEERS, byes)
check("everyone else plays", len(games) == 9, games)

print("2. Several volunteers: the latest to sign up sits out")
check("Steve Green (last volunteer to sign up) has the BYE", byes == ["Steve Green"], byes)

print("3. A volunteer who sat out last session is spared if another can")
setup(REAL, last_week_byes=(18,))  # Steve Green
byes, _ = run()
check("Steve is spared, Ben Ramsay sits out", byes == ["Ben Ramsay"], byes)

print("4. Even numbers: no BYE, volunteers play")
setup(REAL[:-1])
byes, games = run()
check("no BYE", byes == [], byes)
check("nine games", len(games) == 9, games)

print("5. Odd numbers, no volunteers: still exactly one BYE")
setup([(n, v, False) for n, v, _ in REAL])
byes, games = run()
check("exactly one BYE", len(byes) == 1, byes)
check("nine games", len(games) == 9, games)

print("6. One volunteer out of three: it's them, whoever they are")
for vol in range(3):
    people = [(f"P{i}", "Casual", i == vol) for i in range(3)]
    setup(people)
    byes, games = run()
    check(f"P{vol} has the BYE", byes == [f"P{vol}"], (byes, games))

print("7. A player history strands can still take the volunteer")
# Only reachable with allow_repeats_when_needed=False: A has recently played
# B, C and E, so the first pass finds nobody for A. D volunteers. The rescue
# pairs A with D rather than sitting both of them out.
setup([("A", "Casual", False), ("B", "Casual", False), ("C", "Casual", False),
       ("D", "Casual", True), ("E", "Casual", False)])
with Session(database.engine) as db:
    for wk, opp in (("09/09/2026", 2), ("02/09/2026", 3), ("26/08/2026", 5)):
        a = Signup(week=wk, system=SYS, player_id=1, player_name="A", club_id=1)
        b = Signup(week=wk, system=SYS, player_id=opp, player_name="BCDE"[opp - 2], club_id=1)
        db.add(a); db.add(b); db.flush()
        db.add(Pairing(week=wk, system=SYS, a_signup_id=a.id, b_signup_id=b.id,
                       status="published", club_id=1))
    db.commit()
    rows = generate(db, WEEK, SYS, allow_repeats_when_needed=False, persist=False, club_id=1)
byes = [r["a_name"] for r in rows if r["b_signup_id"] is None]
games = [(r["a_name"], r["b_name"]) for r in rows if r["b_signup_id"] is not None]
check("A plays D rather than sitting out", ("A", "D") in games or ("D", "A") in games, games)
check("still exactly one BYE", len(byes) == 1, byes)

print("8. persist=True writes the volunteer's BYE row")
setup(REAL)
with Session(database.engine) as db:
    out = generate(db, WEEK, SYS, persist=True, club_id=1)
    bye_rows = [p for p in out if p.b_signup_id is None]
    names = [db.get(Signup, p.a_signup_id).player_name for p in bye_rows]
check("one persisted BYE, for a volunteer", len(names) == 1 and names[0] in VOLUNTEERS, names)
check("ten rows", len(out) == 10, len(out))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASSED")
