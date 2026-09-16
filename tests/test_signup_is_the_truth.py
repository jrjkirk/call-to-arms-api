"""A signup says what the player asked for. Editing a pairing never rewrites it.

Nick reported it on 16/09/2026: nudging a game's time in the admin grid changed
the times in the signup list above, so the list stopped saying what anyone had
actually asked for. The grid was writing its own displayed values (the later of
the two ETAs, the lower points, the shared vibe, the faction) back onto both
Signup rows.

Now a pairing carries its own eta / points / a_vibe / b_vibe, NULL meaning
"follow the signups". Only PATCH /admin/signups/{id} may change a signup.

Run: PYTHONPATH=. python tests/test_signup_is_the_truth.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime

_DB = pathlib.Path(tempfile.mkdtemp()) / "signup_truth.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import admin  # noqa: E402
import auth  # noqa: E402
import database  # noqa: E402
import signups as signups_mod  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, Pairing, Player, PublishState, Signup, SystemConfig, User,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
WEEK = "23/09/2026"
signups_mod._post_webhook = lambda *a, **k: None
admin._post_webhook = lambda *a, **k: None

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
                        uses_points=True, default_points=2000, max_points=3000,
                        vibe_options=["Casual", "Competitive", "Open", "Intro"], default_vibe="Casual"))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    db.add(User(id=1, discord_id="admin", discord_name="Admin", club_id=1, home_club_id=1, is_super_admin=True))
    for i, name in enumerate(["Nick", "Vince"], start=1):
        db.add(User(id=i + 1, discord_id=f"d{i}", discord_name=name, club_id=1, home_club_id=1))
        db.add(Player(id=i, name=name, club_id=1, user_id=i + 1, active=True))
    # What each player asked for. Deliberately different, as Nick's and Vince's were.
    db.add(Signup(id=1, week=WEEK, system=TOW, player_id=1, player_name="Nick", faction="Skaven",
                  points=2000, eta="18:15", vibe="Casual", club_id=1, created_at=datetime(2026, 9, 1, 12, 0)))
    db.add(Signup(id=2, week=WEEK, system=TOW, player_id=2, player_name="Vince", faction="Empire of Man",
                  points=1500, eta="19:00", vibe="Competitive", club_id=1, created_at=datetime(2026, 9, 1, 12, 5)))
    db.add(Pairing(id=1, week=WEEK, system=TOW, a_signup_id=1, b_signup_id=2, status="pending", club_id=1))
    db.add(PublishState(week=WEEK, system=TOW, published=True, club_id=1))
    db.commit()

ADMIN = TestClient(app)
ADMIN.cookies.set("cta_session", auth._make_session_cookie(1))
ANON = TestClient(app)
H = {"origin": "https://egnwgc.calltoarms.app"}


def signup_list():
    """The signups list on the Pairings tab: the one Nick watched change."""
    r = ADMIN.get("/admin/signups", params={"system": TOW, "week": WEEK}, headers=H)
    if r.status_code != 200:
        return {}
    body = r.json()
    rows = body["signups"] if isinstance(body, dict) else body
    return {s["player_name"]: s for s in rows}


def grid_row():
    r = ADMIN.get("/admin/pairings", params={"system": TOW, "week": WEEK}, headers=H)
    return r.json()["rows"][0] if r.status_code == 200 else {}


def public_row():
    r = ANON.get("/pairings", params={"system": TOW, "week": WEEK, "club": "egnwgc"})
    return r.json()["matchups"][0] if r.status_code == 200 and r.json().get("matchups") else {}


def stored(signup_id):
    with Session(database.engine) as db:
        su = db.get(Signup, signup_id)
        return (su.eta, su.points, su.vibe, su.faction)


def save(**over):
    row = {"id": 1, "a_signup_id": 1, "b_signup_id": 2,
           "a_faction": "Skaven", "b_faction": "Empire of Man",
           "eta": "19:00", "points": 1500, "type": "", "a_type": "", "b_type": ""}
    row.update(over)
    return ADMIN.post("/admin/pairings/save", json={"system": TOW, "week": WEEK, "rows": [row]}, headers=H)


BEFORE = (stored(1), stored(2))
print("\n1. Before touching anything")
check("the signup list shows what each asked for",
      (signup_list().get("Nick", {}).get("eta"), signup_list().get("Vince", {}).get("eta")) == ("18:15", "19:00"),
      str(signup_list())[:200])
check("the game reads as the later of the two", grid_row().get("eta") == "19:00", str(grid_row())[:160])
check("and the public card agrees", public_row().get("eta") == "19:00", str(public_row())[:160])


print("\n2. Nudging the game's time in the grid (the reported bug)")
r = save(eta="20:30")
check("the save succeeds", r.status_code == 200, r.text[:150])
check("the signup list still says 18:15 and 19:00",
      (signup_list()["Nick"]["eta"], signup_list()["Vince"]["eta"]) == ("18:15", "19:00"),
      f'{signup_list()["Nick"]["eta"]} / {signup_list()["Vince"]["eta"]}')
check("neither signup row changed at all", (stored(1), stored(2)) == BEFORE, f"{stored(1)} {stored(2)}")
check("the game now reads 20:30", grid_row().get("eta") == "20:30", str(grid_row().get("eta")))
check("and so does the public card", public_row().get("eta") == "20:30", str(public_row().get("eta")))
with Session(database.engine) as db:
    check("the time is stored on the pairing", db.get(Pairing, 1).eta == "20:30")


print("\n3. Points and vibe behave the same way")
r = save(eta="20:30", points=3000, type="Competitive")
check("the save succeeds", r.status_code == 200, r.text[:150])
check("the signups keep their own points and vibes",
      (stored(1), stored(2)) == BEFORE, f"{stored(1)} {stored(2)}")
check("the game says 3000", grid_row().get("points") == "3000", str(grid_row().get("points")))
check("and Competitive", grid_row().get("type") == "Competitive", str(grid_row().get("type")))
check("the public card matches",
      (public_row().get("points"), public_row().get("game_type")) == ("3000", "Competitive"),
      str(public_row())[:160])


print("\n4. Clearing an override hands the game back to the signups")
r = save(eta="", points="", type="")
check("the save succeeds", r.status_code == 200, r.text[:150])
check("the game is the later signup time again", grid_row().get("eta") == "19:00", str(grid_row().get("eta")))
check("points follow the signups again", grid_row().get("points") == "1500", str(grid_row().get("points")))
check("and the signups are still untouched", (stored(1), stored(2)) == BEFORE, f"{stored(1)} {stored(2)}")


print("\n5. A signup only changes where a signup is edited")
r = ADMIN.patch("/admin/signups/1", json={"eta": "17:45"}, headers=H)
check("an admin editing the signup works", r.status_code == 200, r.text[:150])
check("the signup list shows the new time", signup_list()["Nick"]["eta"] == "17:45", str(signup_list()["Nick"]["eta"]))
check("Vince's is untouched", signup_list()["Vince"]["eta"] == "19:00")
check("and the game, with no override, follows them", grid_row().get("eta") == "19:00", str(grid_row().get("eta")))


print("\n6. A cleared faction stays cleared, and isn't taken off the signup")
r = save(a_faction="— None —")
check("the save succeeds", r.status_code == 200, r.text[:150])
check("the grid shows no faction for Nick", grid_row().get("a_faction") in (None, ""), str(grid_row().get("a_faction")))
check("the public card shows none either", public_row().get("player_a_faction") in (None, ""), str(public_row().get("player_a_faction")))
with Session(database.engine) as db:
    check("but his signup still says Skaven", db.get(Signup, 1).faction == "Skaven", str(db.get(Signup, 1).faction))


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
