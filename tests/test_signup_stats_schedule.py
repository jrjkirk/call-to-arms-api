"""GET /signups/stats: `pairings_post`, when pairings go up.

What must hold:
  - None while the system's auto-pairings are off (the form says nothing)
  - the admin's day and time once they are on
  - it is per system: another system's schedule never leaks across

Run: PYTHONPATH=. python tests/test_signup_stats_schedule.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "stats_schedule.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
from database import system_setting_slug, upsert_setting  # noqa: E402
from main import app  # noqa: E402
from models import Club, ClubSystem, Player, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW, KT = "The Old World", "Kill Team"
SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True))
    db.add(SystemConfig(id=2, name=KT, slug="kt", legacy_system_name=KT, active=True))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly"))
    db.add(ClubSystem(id=2, club_id=1, system_id=2, enabled=True, session_day="Friday", session_cadence="weekly"))
    db.add(User(id=1, display_name="Dee", club_id=1, home_club_id=1))
    db.add(Player(id=1, name="Dee", club_id=1, user_id=1, active=True))
    db.commit()

c = TestClient(app)
c.cookies.set("cta_session", auth._make_session_cookie(1))


def schedule(system):
    r = c.get("/signups/stats", params={"system": system, "week": "28/10/2026"})
    assert r.status_code == 200, r.text
    return r.json()["pairings_post"]


check("off by default: nothing to say", schedule(TOW) is None, schedule(TOW))

with Session(database.engine) as db:
    slug = system_setting_slug(TOW)
    upsert_setting(db, 1, f"auto_pairings_{slug}_enabled", "true")
    upsert_setting(db, 1, f"auto_pairings_{slug}_day", "Tuesday")
    upsert_setting(db, 1, f"auto_pairings_{slug}_time", "19:30")
    db.commit()

check("on: the admin's day and time", schedule(TOW) == {"day": "Tuesday", "time": "19:30"}, schedule(TOW))
check("per system: Kill Team still says nothing", schedule(KT) is None, schedule(KT))

with Session(database.engine) as db:
    upsert_setting(db, 1, f"auto_pairings_{system_setting_slug(TOW)}_enabled", "false")
    db.commit()
check("switched off again: nothing", schedule(TOW) is None, schedule(TOW))

print("\n" + ("ALL PASSED" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
