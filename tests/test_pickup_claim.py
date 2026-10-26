"""POST /pickups/{id}/claim: take up someone's open pick-up request.

What must hold:
  - claiming makes a proposed game straight away, already accepted on the
    claimer's side, at the requester's time and place; the requester is told
  - the requester accepting confirms it
  - a request already taken can't be claimed again
  - not your own request, not someone you're blocked with, and not while you
    have a live request of your own for that system
  - the claimer's faction and points are theirs; the kind of game is the
    requester's

Run: PYTHONPATH=. python tests/test_pickup_claim.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "pickup_claim.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import pickups  # noqa: E402
from main import app  # noqa: E402
from models import Club, ClubSystem, PairingBlock, PickupMatch, PickupRequest, Player, PlayerNotice, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
H = {"origin": "https://egnwgc.calltoarms.app"}
pickups._post_webhook = lambda *a, **k: None
pickups.require_discord_member = lambda *a, **k: None

SQLModel.metadata.create_all(database.engine)
NAMES = {1: "Dee", 2: "Gail", 3: "Pat", 4: "Wally", 5: "Vince"}
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
                        uses_points=True, default_points=2000, max_points=5000,
                        vibe_options=["Casual", "Competitive"], default_vibe="Casual"))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly"))
    for uid, name in NAMES.items():
        db.add(User(id=uid, display_name=name, club_id=1, home_club_id=1))
        db.add(Player(id=uid, name=name, club_id=1, user_id=uid, active=True))
    db.add(PairingBlock(player_a_id=4, player_b_id=5, club_id=1))
    db.commit()

C = {}
for uid in NAMES:
    C[uid] = TestClient(app)
    C[uid].cookies.set("cta_session", auth._make_session_cookie(uid))
day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")


def ask(uid, **extra):
    body = {"system": TOW, "date": day, "start": "18:00", "end": "22:00", "venue": "club", "vibe": "Competitive", "points": 2000}
    body.update(extra)
    return C[uid].post("/pickups", json=body, headers=H)


def claim(uid, request_id, **body):
    return C[uid].post(f"/pickups/{request_id}/claim", json=body, headers=H)


dee = ask(1).json()
check("Dee's request is open", dee["status"] == "open", dee)

check("can't claim your own request", claim(1, dee["id"]).status_code == 422)

r = claim(2, dee["id"], faction="Skaven", points=1500)
check("Gail claims it", r.status_code == 200, r.text)
g = r.json()
check("a game is proposed straight away", g["match"] and g["match"]["status"] == "proposed", g)
check("already accepted on Gail's side", g["match"]["my_answer"] == "accepted" and g["match"]["their_answer"] == "pending", g["match"])
check("at Dee's time and place", g["window_start"] == dee["window_start"] and g["where"] == dee["where"], (g["where"], dee["where"]))
check("Gail's own faction and points; Dee's kind of game",
      (g["faction"], g["points"], g["vibe"]) == ("Skaven", 1500, "Competitive"), (g["faction"], g["points"], g["vibe"]))
with Session(database.engine) as db:
    told = db.exec(select(PlayerNotice).where(PlayerNotice.user_id == 1)).all()
check("Dee is told who wants to play", any("Gail wants to play" in n.body for n in told), [n.body for n in told])

check("a request already taken can't be claimed again", claim(3, dee["id"]).status_code == 409)

others = C[3].get(f"/pickups?system={TOW}", headers=H).json()["others"]
check("and it's gone from the open list", all(o["id"] != dee["id"] for o in others), others)

mid = g["match"]["id"]
r = C[1].post(f"/pickups/matches/{mid}/accept", json={}, headers=H)
check("Dee accepts: the game is on", r.status_code == 200 and r.json()["status"] == "confirmed", r.text)

# Blocks hold for claims too.
wally = ask(4).json()
check("blocked players can't claim each other", claim(5, wally["id"]).status_code == 409)

# One live request per player per system.
pat = ask(3, venue="other", venue_text="Pat's place").json()  # a place nobody else picked, so it stays open
check("Gail, with a game already on, can't claim another", claim(2, pat["id"]).status_code == 409)

# Declining a claim: the claimer goes back in the pool, the requester leaves it.
r = claim(5, pat["id"])
check("Vince claims Pat's", r.status_code == 200, r.text)
mid = r.json()["match"]["id"]
C[3].post(f"/pickups/matches/{mid}/decline", json={"reason": "busy"}, headers=H)
with Session(database.engine) as db:
    vince = db.exec(select(PickupRequest).where(PickupRequest.player_id == 5)).first()
    pat_req = db.get(PickupRequest, pat["id"])
check("Pat declining puts Vince back in the pool", vince.status == "open", vince.status)
check("and closes Pat's request", pat_req.status == "declined", pat_req.status)

print("\n" + ("ALL PASSED" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
