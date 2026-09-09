"""Blocks that belong to one game system, and the roster behind them.

A block used to mean "never pair these two, anywhere". That is still what every
block written before 2026-09-08 means, and the first block below is the one
that matters most: a club that never scopes a block must pair EXACTLY as it did
before, because the matcher is a faithful port and deliberately frozen.

The rest covers the thing that was newly possible: a system admin saying "these
two have met four times in Kill Team" without that leaking into the club's Old
World night.

Run: PYTHONPATH=. python tests/test_per_system_blocks.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "blocks.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, PairingBlock, Pairing, Player, Signup, SystemConfig,
)
from pairings_engine import generate  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
KT = "Kill Team"
WEEK = "10/09/2026"

SQLModel.metadata.create_all(database.engine)

# Four players who all turn up to both game nights, so the only thing that can
# separate the two systems' pairings is the block itself.
PEOPLE = [(1, "Ann"), (2, "Bob"), (3, "Cat"), (4, "Dan")]

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc"))
    for sid, name, slug in ((1, TOW, "tow"), (2, KT, "kt")):
        db.add(SystemConfig(id=sid, name=name, slug=slug, legacy_system_name=name,
                            active=True, uses_points=(slug == "tow"),
                            vibe_options=["Casual", "Competitive", "Open"],
                            default_vibe="Open"))
        db.add(ClubSystem(id=sid, club_id=1, system_id=sid, enabled=True,
                          session_day="Thursday", session_cadence="weekly"))
    for pid, name in PEOPLE:
        # club_id matters here: the roster reads players through scoped(),
        # so a player row without one is invisible to it.
        db.add(Player(id=pid, name=name, club_id=1))
        for system in (TOW, KT):
            db.add(Signup(week=WEEK, system=system, player_id=pid, player_name=name,
                          faction="Empire of Man", points=2000, eta="18:00",
                          vibe="Open", club_id=1))
    db.commit()


def pairs(system):
    """Generate and return the pairings as name pairs, order-insensitive."""
    with Session(database.engine) as db:
        rows = generate(db, WEEK, system, persist=False, club_id=1)
    out = []
    for r in rows:
        a = r.get("a_name") or r.get("player_a_name") or r.get("A")
        b = r.get("b_name") or r.get("player_b_name") or r.get("B")
        out.append(frozenset(x for x in (a, b) if x))
    return out


def set_blocks(*rows):
    """Replace every block with the given (a_id, b_id, system_id) tuples."""
    with Session(database.engine) as db:
        for b in db.exec(select(PairingBlock)).all():
            db.delete(b)
        for a, b_, sid in rows:
            lo, hi = sorted([a, b_])
            db.add(PairingBlock(player_a_id=lo, player_b_id=hi, club_id=1, system_id=sid))
        db.commit()


print("\n1. A club that never scopes a block is untouched")
set_blocks()
baseline_tow = pairs(TOW)
baseline_kt = pairs(KT)
check("everyone gets a game in both systems",
      sum(len(p) for p in baseline_tow) == 4 and sum(len(p) for p in baseline_kt) == 4,
      f"{baseline_tow} / {baseline_kt}")

# system_id NULL is the shape every pre-existing row has.
set_blocks((1, 2, None))
club_wide_tow = pairs(TOW)
club_wide_kt = pairs(KT)
check("a club-wide block still applies to The Old World",
      frozenset({"Ann", "Bob"}) not in club_wide_tow, str(club_wide_tow))
check("and still applies to Kill Team",
      frozenset({"Ann", "Bob"}) not in club_wide_kt, str(club_wide_kt))
check("everyone still gets a game either side of it",
      sum(len(p) for p in club_wide_tow) == 4, str(club_wide_tow))


print("\n2. A block scoped to one system stays in it")
set_blocks((1, 2, 2))  # Kill Team only
scoped_tow = pairs(TOW)
scoped_kt = pairs(KT)
check("Kill Team keeps Ann and Bob apart",
      frozenset({"Ann", "Bob"}) not in scoped_kt, str(scoped_kt))
check("The Old World is unaffected by it",
      scoped_tow == baseline_tow, f"{scoped_tow} vs baseline {baseline_tow}")
check("and everyone still gets a game in Kill Team",
      sum(len(p) for p in scoped_kt) == 4, str(scoped_kt))


print("\n3. Two systems block independently")
set_blocks((1, 2, 2), (1, 3, 1))  # Ann/Bob in KT, Ann/Cat in TOW
both_tow = pairs(TOW)
both_kt = pairs(KT)
check("The Old World honours only its own block",
      frozenset({"Ann", "Cat"}) not in both_tow and frozenset({"Ann", "Bob"}) in both_tow,
      str(both_tow))
check("Kill Team honours only its own block",
      frozenset({"Ann", "Bob"}) not in both_kt and frozenset({"Ann", "Cat"}) in both_kt,
      str(both_kt))

# A block on a system this club no longer runs must not silently apply
# everywhere. Pointing one at a system id that does not match either run is the
# cheapest way to assert the filter is on equality, not on truthiness.
print("\n4. A block belonging to some other system is ignored")
set_blocks((1, 2, 99))
stray = pairs(TOW)
check("a foreign system_id blocks nothing here", stray == baseline_tow, str(stray))


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------
print("\n5. Authorization: who may write which kind of block")
from fastapi.testclient import TestClient  # noqa: E402

import admin  # noqa: E402
import auth  # noqa: E402
from main import app  # noqa: E402
from models import AdminRole, User  # noqa: E402

with Session(database.engine) as db:
    db.add(User(id=1, discord_id="1", discord_name="boss", player_id=None,
                club_id=1, home_club_id=1, is_super_admin=True))
    db.add(User(id=2, discord_id="2", discord_name="ktadmin", player_id=None,
                club_id=1, home_club_id=1, is_super_admin=False))
    db.add(AdminRole(user_id=2, scope=KT, club_id=1))
    db.commit()

client = TestClient(app)


def as_user(uid: int):
    """Impersonate by overriding the admin dependencies themselves.

    Overriding require_user lower down does not work: require_admin then runs
    _rebase_admin, which expunges the user from the session it was loaded in,
    and a detached instance is not in any session. These endpoints are being
    tested for their own authorization branch, not for the re-basing, which
    the multi-club tests already cover.
    """
    with Session(database.engine) as db:
        row = db.get(User, uid)
        u = User(id=row.id, discord_id=row.discord_id, discord_name=row.discord_name,
                 player_id=row.player_id, club_id=row.club_id,
                 home_club_id=row.home_club_id, is_super_admin=row.is_super_admin,
                 is_platform_admin=row.is_platform_admin)
    app.dependency_overrides[auth.require_admin] = lambda: u
    app.dependency_overrides[auth.require_super_admin] = lambda: u
    app.dependency_overrides[auth.active_club_id] = lambda: 1
    return u


set_blocks()

as_user(2)  # Kill Team scope admin, not a super-admin
r = client.post("/admin/blocks", json={"player_a_id": 1, "player_b_id": 2})
check("a scope admin cannot create a club-wide block", r.status_code == 403, r.text)

r = client.post("/admin/blocks", json={"player_a_id": 1, "player_b_id": 2, "system": KT})
check("but can block a pair in their own system", r.status_code == 200, r.text)

r = client.post("/admin/blocks", json={"player_a_id": 1, "player_b_id": 2, "system": TOW})
check("and cannot block a pair in a system they don't run", r.status_code == 403, r.text)

as_user(1)  # super-admin
r = client.post("/admin/blocks", json={"player_a_id": 1, "player_b_id": 2})
check("a super-admin can create the club-wide one", r.status_code == 200, r.text)

with Session(database.engine) as db:
    rows = db.exec(select(PairingBlock)).all()
check("both blocks coexist on the same pair", len(rows) == 2, str([(b.system_id) for b in rows]))
check("one club-wide, one scoped",
      sorted([b.system_id is None for b in rows]) == [False, True],
      str([b.system_id for b in rows]))


print("\n6. The system tab sees its own blocks and the club-wide ones")
r = client.get(f"/admin/blocks?system={KT}")
kt_blocks = r.json()
check("Kill Team sees both", len(kt_blocks) == 2, str(kt_blocks))
check("and is told which it may not remove",
      sorted(b["club_wide"] for b in kt_blocks) == [False, True], str(kt_blocks))

r = client.get(f"/admin/blocks?system={TOW}")
tow_blocks = r.json()
check("The Old World sees only the club-wide one",
      len(tow_blocks) == 1 and tow_blocks[0]["club_wide"], str(tow_blocks))

r = client.get("/admin/blocks")
check("the club view sees everything", len(r.json()) == 2, r.text)
check("and names the system a scoped block belongs to",
      sorted(str(b["system"]) for b in r.json()) == ["Kill Team", "None"], r.text)


print("\n7. Deleting respects the same split")
as_user(2)
r = client.delete(f"/admin/blocks?player_a_id=1&player_b_id=2&system={KT}")
check("a scope admin can lift their own block",
      r.status_code == 200 and r.json()["removed"], r.text)
r = client.delete("/admin/blocks?player_a_id=1&player_b_id=2")
check("but not the club-wide one", r.status_code == 403, r.text)
with Session(database.engine) as db:
    check("which is still there",
          db.exec(select(PairingBlock)).one().system_id is None)


print("\n8. The roster is derived from signups, not a membership table")
as_user(2)
r = client.get(f"/admin/system-players?system={KT}")
roster = r.json()
check("everyone who signed up is on it", len(roster) == 4, str(roster))
check("with the fields the tab shows",
      all({"name", "games", "level", "experience", "extra_games", "rating"} <= set(p)
          for p in roster),
      str(roster[0] if roster else None))

r = client.get(f"/admin/system-players?system={TOW}")
check("and a scope admin cannot read another system's roster", r.status_code == 403, r.text)

print("\n9. The roster carries the Discord handle behind each player")
# Both links are populated in the wild: Player.user_id is the multi-club
# ownership link, User.player_id the older home-club one. A roster that reads
# only one leaves half the column blank, so both are seeded and both asserted.
with Session(database.engine) as db:
    db.add(User(id=3, discord_id="3", discord_name="ann_plays", player_id=None,
                club_id=1, home_club_id=1, is_super_admin=False))
    db.add(User(id=4, discord_id="4", discord_name="bob_legacy", player_id=2,
                club_id=1, home_club_id=1, is_super_admin=False))
    db.flush()
    db.get(Player, 1).user_id = 3          # new-style ownership
    db.commit()

by_id = {p["player_id"]: p for p in client.get(f"/admin/system-players?system={KT}").json()}
check("a player claimed through Player.user_id shows their handle",
      by_id[1]["discord_name"] == "ann_plays", str(by_id[1]))
check("and one linked the old way through User.player_id does too",
      by_id[2]["discord_name"] == "bob_legacy", str(by_id[2]))
check("an unclaimed roster entry is null, not a guess",
      by_id[3]["discord_name"] is None, str(by_id[3]))


print("\n10. Titles can be set from a system's roster")
check("they start empty", by_id[1]["titles"] == [], str(by_id[1]["titles"]))
r = client.post("/admin/system-players/titles",
                json={"system": KT, "player_id": 1, "titles": ["Kill Team Champion 2026", " ", "Best Painted"]})
check("a scope admin can award one", r.status_code == 200, r.text)
check("blank lines are dropped",
      r.json()["titles"] == ["Kill Team Champion 2026", "Best Painted"], r.text)

again = {p["player_id"]: p for p in client.get(f"/admin/system-players?system={KT}").json()}
check("and the roster shows them back",
      again[1]["titles"] == ["Kill Team Champion 2026", "Best Painted"], str(again[1]["titles"]))

check("a system they don't run is refused",
      client.post("/admin/system-players/titles",
                  json={"system": TOW, "player_id": 1, "titles": ["x"]}).status_code == 403)
check("an over-long title is refused",
      client.post("/admin/system-players/titles",
                  json={"system": KT, "player_id": 1, "titles": ["x" * 81]}).status_code == 422)
check("and too many of them",
      client.post("/admin/system-players/titles",
                  json={"system": KT, "player_id": 1, "titles": [f"t{i}" for i in range(21)]}).status_code == 422)

# The endpoint exists so a SCOPE admin can award a title. It must not become a
# second way to edit the club-level fields that PATCH /players guards.
with Session(database.engine) as db:
    p1 = db.get(Player, 1)
    check("it touches nothing else on the player",
          p1.name == "Ann" and p1.active is True and p1.league_visible is True)

r = client.post("/admin/system-players/titles",
                json={"system": KT, "player_id": 1, "titles": []})
check("clearing them back to none works", r.json()["titles"] == [], r.text)


print("\n11. Declared games move the tier, never the level")
before = {p["player_id"]: p for p in roster}[1]
r = client.post("/admin/system-players/experience",
                json={"system": KT, "player_id": 1, "extra_games": 25})
check("an admin can set games played elsewhere", r.status_code == 200, r.text)
after = {p["player_id"]: p for p in client.get(f"/admin/system-players?system={KT}").json()}[1]
check("the tier moves", after["experience"] != before["experience"],
      f"{before['experience']} -> {after['experience']}")
check("the level does not", after["level"] == before["level"],
      f"{before['level']} -> {after['level']}")
check("negative is refused",
      client.post("/admin/system-players/experience",
                  json={"system": KT, "player_id": 1, "extra_games": -1}).status_code == 422)

app.dependency_overrides.clear()

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
