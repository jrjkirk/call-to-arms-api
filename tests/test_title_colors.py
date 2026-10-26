"""Player titles can carry a colour an admin chose.

What must hold:
  - titles saved before colours (a plain JSON list of names) read unchanged
  - POST /admin/system-players/titles with colors saves them, and GET
    /players/{id} returns them as title_colors
  - a bad colour is ignored, never stored
  - PATCH /players (names only) keeps each surviving title's colour

Run: PYTHONPATH=. python tests/test_title_colors.py
"""
import json
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "title_colors.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
from main import app  # noqa: E402
from models import Club, ClubSystem, Player, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
ORIGIN = {"origin": "https://egnwgc.calltoarms.app"}
SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly"))
    db.add(User(id=1, display_name="Dee", club_id=1, home_club_id=1, is_super_admin=True))
    db.add(Player(id=1, name="Gail", club_id=1, active=True, titles=json.dumps(["Champion 2025", "Painter"])))
    db.commit()

c = TestClient(app)
c.cookies.set("cta_session", auth._make_session_cookie(1))


def profile():
    return c.get("/players/1", headers=ORIGIN).json()


p = profile()
check("old plain titles read unchanged", p["titles"] == ["Champion 2025", "Painter"], p.get("titles"))
check("old titles have no colours", p["title_colors"] == {}, p.get("title_colors"))

r = c.post("/admin/system-players/titles", json={
    "system": TOW, "player_id": 1, "titles": ["Champion 2025", "Painter", "Hobbyist"],
    "colors": {"Champion 2025": "#c9a14a", "Painter": "not-a-colour"},
})
check("save with colours ok", r.status_code == 200, r.text)
p = profile()
check("titles in order", p["titles"] == ["Champion 2025", "Painter", "Hobbyist"], p["titles"])
check("good colour kept, bad one ignored", p["title_colors"] == {"Champion 2025": "#c9a14a"}, p["title_colors"])

r = c.patch("/admin/players/1", json={"titles": ["Champion 2025", "Hobbyist"]}, headers=ORIGIN)
check("names-only save ok", r.status_code == 200, r.text)
p = profile()
check("names-only save keeps the surviving colour", p["title_colors"] == {"Champion 2025": "#c9a14a"}, p["title_colors"])
check("removed title gone", p["titles"] == ["Champion 2025", "Hobbyist"], p["titles"])

print("\n" + ("ALL PASSED" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
