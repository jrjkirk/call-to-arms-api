"""A club's own answers about a system it runs.

SystemConfig is shared by every club, so until now two clubs running The Old
World had to agree on 2000 points, and a club playing Kill Team to a scenario
pack had no way to say so. Nine fields on ClubSystem now let a system's own
admin answer for their own night.

Block 1 is the one that matters: a club that has overridden nothing must be
byte-identical to before, because NULL everywhere is the state every existing
row is in. Block 5 is the one that would be easiest to get wrong: an override
that reaches the signup form but not the matcher is worse than no override.

Run: PYTHONPATH=. python tests/test_club_system_overrides.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "overrides.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
from main import app  # noqa: E402
from models import Club, ClubSystem, Player, Signup, SystemConfig, User  # noqa: E402
from pairings_engine import generate  # noqa: E402
from system_overrides import effective_system  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
WEEK = "24/09/2026"

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(Club(id=2, name="Other Club", slug="other", active=True))
    db.add(User(id=1, discord_id="a", discord_name="Joel", club_id=1,
                home_club_id=1, is_super_admin=True))
    db.add(SystemConfig(
        id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
        uses_points=True, default_points=2000, max_points=3000,
        uses_scenarios=True, scenario_options=["Open Battle", "Weekly Scenario"],
        default_scenario="Open Battle", allows_demo=True, uses_standby=True,
        has_intro_prepass=True,
        vibe_options=["Casual", "Competitive", "Open"], default_vibe="Open",
    ))
    # A system the catalogue gives no scenarios at all, so "turn scenarios on"
    # with no list of its own has genuinely nothing to fall back to.
    db.add(SystemConfig(
        id=2, name="Kill Team", slug="kt", legacy_system_name="Kill Team",
        active=True, uses_points=False, uses_scenarios=False,
        scenario_options=None, default_scenario=None,
        vibe_options=["Standard"], default_vibe="Standard",
    ))
    for cid in (1, 2):
        db.add(ClubSystem(club_id=cid, system_id=1, enabled=True,
                          session_day="Thursday", session_cadence="weekly"))
    db.add(ClubSystem(club_id=1, system_id=2, enabled=True,
                      session_day="Tuesday", session_cadence="weekly"))
    db.commit()

client = TestClient(app)
client.cookies.set("cta_session", auth._make_session_cookie(1))


def catalogue_row():
    with Session(database.engine) as db:
        return db.get(SystemConfig, 1)


def eff(club_id):
    with Session(database.engine) as db:
        return effective_system(db, club_id, db.get(SystemConfig, 1))


def save_overrides(overrides):
    return client.post("/admin/club-systems", json={
        "system_id": 1, "enabled": True,
        "session_day": "Thursday", "session_cadence": "weekly",
        "overrides": overrides,
    })


print("\n1. A club that has overridden nothing is exactly the catalogue")
e = eff(1)
sc = catalogue_row()
same = all(getattr(e, f) == getattr(sc, f) for f in
           ("uses_points", "default_points", "max_points", "uses_scenarios",
            "scenario_options", "default_scenario", "allows_demo",
            "uses_standby", "has_intro_prepass"))
check("every overridable field matches the catalogue", same)
check("and so does everything it does not own", e.id == sc.id and e.slug == "tow")
check("it reports nothing as customised", e.overridden_fields == [], str(e.overridden_fields))

r = client.get(f"/systems?club=egnwgc")
row = next(s for s in r.json() if s["slug"] == "tow")
check("the public catalogue serves the platform values",
      row["default_points"] == 2000 and row["max_points"] == 3000, str(row["default_points"]))


print("\n2. One club's override does not touch another's")
r = save_overrides({"default_points": 1500, "max_points": 2000})
check("saved", r.status_code == 200, r.text[:200])
check("this club sees its own numbers",
      (eff(1).default_points, eff(1).max_points) == (1500, 2000), repr((eff(1).default_points, eff(1).max_points)))
check("the other club still sees the catalogue",
      (eff(2).default_points, eff(2).max_points) == (2000, 3000), repr((eff(2).default_points, eff(2).max_points)))
check("and the catalogue row itself is untouched",
      (catalogue_row().default_points, catalogue_row().max_points) == (2000, 3000))
check("only the two fields set are reported as customised",
      eff(1).overridden_fields == ["default_points", "max_points"], str(eff(1).overridden_fields))


print("\n3. The platform default stays live for what a club has not set")
# The reason nothing is backfilled: a club that never answered still follows a
# catalogue correction.
with Session(database.engine) as db:
    sc = db.get(SystemConfig, 1)
    sc.allows_demo = False
    db.add(sc)
    db.commit()
check("a catalogue change reaches the club that overrode other fields",
      eff(1).allows_demo is False)
with Session(database.engine) as db:
    sc = db.get(SystemConfig, 1)
    sc.allows_demo = True
    db.add(sc)
    db.commit()


print("\n4. False is an answer, not an absence")
# The whole reason the boolean columns are nullable rather than DEFAULT FALSE.
r = save_overrides({"uses_points": False})
check("saved", r.status_code == 200, r.text[:200])
check("the club now uses no points, though the catalogue does",
      eff(1).uses_points is False and catalogue_row().uses_points is True)
check("and that counts as customised, not as unset",
      "uses_points" in eff(1).overridden_fields, str(eff(1).overridden_fields))

r = save_overrides({"uses_points": None})
check("clearing it goes back to the catalogue",
      r.status_code == 200 and eff(1).uses_points is True, r.text[:120])
check("and it is no longer reported as customised",
      "uses_points" not in eff(1).overridden_fields, str(eff(1).overridden_fields))


print("\n5. The override reaches the MATCHER, not just the form")
# An override honoured by the signup form and ignored by pairings_engine would
# be worse than none: a club with scenarios off would get a form with no
# scenario field and a matcher still scoring agreement between two blanks.
with Session(database.engine) as db:
    for pid, name in ((1, "Ann"), (2, "Bob")):
        db.add(Player(id=pid, name=name, club_id=1))
        db.add(Signup(week=WEEK, system=TOW, player_id=pid, player_name=name,
                      faction="Skaven", points=2000, eta="18:00", vibe="Open",
                      scenario="Open Battle", club_id=1))
    db.commit()

save_overrides({"uses_scenarios": False, "has_intro_prepass": False})
import pairings_engine  # noqa: E402
seen = {}
real_pair_dist = pairings_engine._pair_dist


def spy(ms, other, system, *a, **kw):
    # Found by shape rather than by index, so a future argument added to
    # _pair_dist does not turn this into a silently-passing test.
    cfg = kw.get("config") or next(x for x in a if hasattr(x, "uses_scenarios"))
    seen["uses_scenarios"] = cfg.uses_scenarios
    seen["has_intro_prepass"] = cfg.has_intro_prepass
    return real_pair_dist(ms, other, system, *a, **kw)


pairings_engine._pair_dist = spy
with Session(database.engine) as db:
    generate(db, WEEK, TOW, persist=False, club_id=1)
pairings_engine._pair_dist = real_pair_dist
check("the matcher saw the club's scenarios-off override",
      seen.get("uses_scenarios") is False, str(seen))
check("and its intro-prepass override", seen.get("has_intro_prepass") is False, str(seen))

save_overrides({"uses_scenarios": None, "has_intro_prepass": None})


print("\n6. A club cannot save a form that cannot be filled in")
# Kill Team's catalogue row has no scenarios, so turning them on without
# supplying a list leaves a dropdown with nothing in it.
r = client.post("/admin/club-systems", json={
    "system_id": 2, "enabled": True, "session_day": "Tuesday",
    "session_cadence": "weekly", "overrides": {"uses_scenarios": True},
})
check("scenarios on with nothing to pick is refused", r.status_code == 422, r.text[:140])
r = client.post("/admin/club-systems", json={
    "system_id": 2, "enabled": True, "session_day": "Tuesday",
    "session_cadence": "weekly",
    "overrides": {"uses_scenarios": True, "scenario_options": ["Into the Dark"]},
})
check("but on WITH a list is fine", r.status_code == 200, r.text[:140])

r = save_overrides({"default_points": 5000})
check("a default above the inherited maximum is refused", r.status_code == 422, r.text[:140])

r = save_overrides({"scenario_options": ["Meeting Engagement", "Ambush"],
                    "default_scenario": "Open Battle"})
check("a default scenario the caller ASKED for and that is not in the list "
      "is refused", r.status_code == 422, r.text[:140])

# Replacing the whole list is the ordinary case, and it must not fail because
# the INHERITED default went stale as a result. That would make swapping a
# club's scenarios impossible in one call.
r = save_overrides({"scenario_options": ["Meeting Engagement", " Ambush ", "", "ambush"]})
check("replacing the list succeeds", r.status_code == 200, r.text[:140])
check("blanks and duplicates are dropped, and the stale inherited default "
      "falls to the first scenario",
      eff(1).scenario_options == ["Meeting Engagement", "Ambush"]
      and eff(1).default_scenario == "Meeting Engagement",
      f"{eff(1).scenario_options} / {eff(1).default_scenario}")

# Clearing the list is a return to the catalogue, not an empty dropdown.
r = save_overrides({"scenario_options": None, "default_scenario": None})
check("clearing the override goes back to the catalogue's scenarios",
      r.status_code == 200 and eff(1).scenario_options == ["Open Battle", "Weekly Scenario"],
      f"{r.status_code} {eff(1).scenario_options}")

r = save_overrides({"faction_list": ["Skaven"]})
check("factions are not a per-club decision", r.status_code == 422, r.text[:140])


print("\n7. The signup form is served the club's answers")
save_overrides({"uses_points": True, "default_points": 1500, "max_points": 2000,
                "uses_standby": False})
row = next(s for s in client.get("/systems?club=egnwgc").json() if s["slug"] == "tow")
check("points come back club-resolved",
      (row["default_points"], row["max_points"]) == (1500, 2000), str(row))
check("and standby", row["uses_standby"] is False, str(row["uses_standby"]))
row2 = next(s for s in client.get("/systems?club=other").json() if s["slug"] == "tow")
check("the other club's form is unchanged",
      (row2["default_points"], row2["max_points"]) == (2000, 3000), str(row2))
unscoped = next(s for s in client.get("/systems").json() if s["slug"] == "tow")
check("and the unscoped catalogue shows the platform values",
      (unscoped["default_points"], unscoped["max_points"]) == (2000, 3000), str(unscoped))


print("\n8. A signup is validated against the club's numbers, not the platform's")
r = client.get("/admin/club-systems")
check("the admin read reports both the override and what it overrides",
      r.status_code == 200
      and next(x for x in r.json() if x["system_id"] == 1)["default_points"] == 1500
      and next(x for x in r.json() if x["system_id"] == 1)["catalogue"]["default_points"] == 2000,
      r.text[:200])

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
