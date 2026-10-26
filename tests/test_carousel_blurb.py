"""The Club page's editable bits: the club night blurb is capped, and the
club's region round-trips through GET /club.

What must hold:
  - a blurb up to CAROUSEL_BLURB_MAX characters saves
  - one longer is refused (422) and the saved blurb is untouched
  - surrounding spaces don't count towards the limit

Run: PYTHONPATH=. python tests/test_carousel_blurb.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "carousel_blurb.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
from admin_club import CAROUSEL_BLURB_MAX  # noqa: E402
from main import app  # noqa: E402
from models import Club, ClubSystem, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly"))
    db.add(User(id=1, display_name="Dee", club_id=1, home_club_id=1, is_super_admin=True))
    db.commit()

c = TestClient(app)
c.cookies.set("cta_session", auth._make_session_cookie(1))


def save(blurb):
    return c.post("/admin/club-systems/carousel", json={"system": TOW, "blurb": blurb})


def saved():
    with Session(database.engine) as db:
        return db.get(ClubSystem, 1).carousel_blurb


ok = "x" * CAROUSEL_BLURB_MAX
r = save(ok)
check("at the limit: saves", r.status_code == 200 and saved() == ok, r.text)
r = save("y" * (CAROUSEL_BLURB_MAX + 1))
check("over the limit: refused", r.status_code == 422, r.status_code)
check("over the limit: the saved blurb is untouched", saved() == ok)
r = save("  " + "z" * CAROUSEL_BLURB_MAX + "  ")
check("spaces round it don't count", r.status_code == 200 and saved() == "z" * CAROUSEL_BLURB_MAX, r.text)

# GET /club carries the region, so the admin Club page (which loads its form
# from it and saves the region back) doesn't clear it on every save.
with Session(database.engine) as db:
    club = db.get(Club, 1)
    club.region = "North West"
    db.add(club)
    db.commit()
r = c.get("/club", headers={"origin": "https://egnwgc.calltoarms.app"})
check("GET /club returns the region", r.status_code == 200 and r.json()["club"].get("region") == "North West", r.text[:200])

print("\n" + ("ALL PASSED" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
