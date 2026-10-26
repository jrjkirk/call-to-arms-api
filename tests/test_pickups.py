"""Pick-up games (pickups.py): matchmaking outside club night.

What must hold:
  - a request posts and joins the pool; bad windows are refused; one live
    request per player per system
  - two compatible requests are proposed to each other
  - never matched: places that disagree, an exclusive kind against another,
    a blocked pair, windows overlapping by under two hours
  - both accept -> the game is on, and only then is each told how to reach
    the other
  - a decline takes the decliner out; the other goes back in and is matched
    again if they can be; the reason is admin-only, never shown to players
  - a proposal nobody answers in time closes the same way (sweep)
  - cancelling a confirmed game puts the other player back in the pool

Run: PYTHONPATH=. python tests/test_pickups.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "pickups.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import pickups  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, PairingBlock, PickupMatch, PickupRequest, Player, PlayerNotice, SystemConfig, User,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
H = {"origin": "https://egnwgc.calltoarms.app"}
POSTS: list[str] = []
pickups._post_webhook = lambda db, club_id, system, content, **k: POSTS.append(content)
# Discord membership gate: not under test here.
pickups.require_discord_member = lambda *a, **k: None

SQLModel.metadata.create_all(database.engine)
NAMES = {1: "Dee", 2: "Gail", 3: "Pat", 4: "Wally", 5: "Vince", 6: "Nate", 7: "Ola", 8: "Ray"}
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
                        uses_points=True, default_points=2000, max_points=5000,
                        vibe_options=["Casual", "Competitive", "Open", "Intro"], default_vibe="Casual"))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly",
                      vibe_options=["Casual", "Competitive", {"name": "Battle March", "behaviour": "exclusive"}]))
    db.add(User(id=99, display_name="Admin", club_id=1, home_club_id=1, is_super_admin=True))
    for uid, name in NAMES.items():
        db.add(User(id=uid, display_name=name, discord_name=f"{name.lower()}_dc", club_id=1, home_club_id=1))
        db.add(Player(id=uid, name=name, club_id=1, user_id=uid, active=True))
    db.add(PairingBlock(player_a_id=7, player_b_id=8, club_id=1))
    db.commit()


def client_for(uid):
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(uid))
    return c


C = {uid: client_for(uid) for uid in NAMES}
ADMIN = client_for(99)
day = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")


def ask(uid, start="18:00", end="22:00", **extra):
    body = {"system": TOW, "date": day, "start": start, "end": end, "venue": "club", "vibe": "Casual", "points": 2000}
    body.update(extra)
    return C[uid].post("/pickups", json=body, headers=H)


def mine(uid):
    return C[uid].get(f"/pickups?system={TOW}", headers=H).json()["mine"]


# ── asking ──
check("end before start refused", ask(1, "20:00", "19:00").status_code == 422)
check("under two hours refused", ask(1, "18:00", "19:30").status_code == 422)
r = ask(1)
check("a request is made and posted to Discord", r.status_code == 200 and r.json()["status"] == "open" and POSTS, r.text)
check("one live request per player per system", ask(1).status_code == 409)

# ── never matched: place, exclusive kind, block, short overlap ──
ask(2, venue="other", venue_text="Gail's place")
ask(3, vibe="Battle March")
ask(4, start="21:00", end="23:30")          # overlaps Dee by only 1 hour
with Session(database.engine) as db:
    proposed = db.exec(select(PickupMatch)).all()
check("nobody incompatible was matched with Dee", proposed == [], proposed)

ask(7)
ask(8)
with Session(database.engine) as db:
    m78 = [m for m in db.exec(select(PickupMatch)).all()]
    by_req = {r.id: r.player_id for r in db.exec(select(PickupRequest)).all()}
pairs = {frozenset((by_req[m.a_request_id], by_req[m.b_request_id])) for m in m78}
check("a blocked pair is never matched", frozenset((7, 8)) not in pairs, pairs)
check("Dee (oldest) is matched with a compatible player", any(1 in p for p in pairs), pairs)

# Who did Dee get? (Ola: Ray's block is with Ola, and Dee is first in the queue.)
dee = mine(1)[0]
check("Dee sees a proposal waiting for an answer", dee["status"] == "proposed" and dee["match"]["my_answer"] == "pending", dee)
partner_id = dee["match"]["opponent"]["player_id"]
check("no contact details before both accept", dee["match"]["opponent"]["discord"] is None)
with Session(database.engine) as db:
    told = db.exec(select(PlayerNotice).where(PlayerNotice.user_id == 1)).all()
check("Dee was told a game was found", any("Pick-up game found" in n.body for n in told), [n.body for n in told])

# ── both accept ──
mid = dee["match"]["id"]
C[1].post(f"/pickups/matches/{mid}/accept", headers=H)
r = C[partner_id].post(f"/pickups/matches/{mid}/accept", headers=H).json()
check("both accepted -> confirmed", r["status"] == "confirmed", r)
dee = mine(1)[0]
check("now each can see how to reach the other", dee["match"]["opponent"]["discord"] == f"{NAMES[partner_id].lower()}_dc", dee)
check("the game is posted to Discord", any("Pick-up game on" in p for p in POSTS))

# ── decline: private reason, decliner out, other back in and re-matched ──
ask(5)
ask(6)
v = mine(5)[0]
check("Vince and Nate proposed to each other", v["status"] == "proposed" and v["match"]["opponent"]["name"] in ("Nate", "Ray"), v)
vid, vopp = v["match"]["id"], v["match"]["opponent"]["player_id"]
C[5].post(f"/pickups/matches/{vid}/decline", json={"reason": "Not keen on that matchup"}, headers=H)
check("the decliner is out of the pool", mine(5)[0]["status"] == "declined" if mine(5) else True)
other = mine(vopp)[0]
check("the other player is back in the pool (or matched again)", other["status"] in ("open", "proposed"), other)
everything = str(C[vopp].get("/pickups", headers=H).json()) + str(C[1].get("/pickups", headers=H).json())
check("the reason is never shown to players", "Not keen" not in everything)
adm = ADMIN.get(f"/admin/pickups?system={TOW}", headers=H)
check("admins see the reason", adm.status_code == 200 and any(x["reason"] == "Not keen on that matchup" and x["declined_by"] == "Vince" for x in adm.json()), adm.text)
check("players can't read the admin view", C[1].get(f"/admin/pickups?system={TOW}", headers=H).status_code == 403)

# ── nobody answers in time ──
with Session(database.engine) as db:
    pending = db.exec(select(PickupMatch).where(PickupMatch.status == "proposed")).all()
    for m in pending:
        m.a_answer = "accepted"            # one side said yes
        m.expires_at = datetime.now() - timedelta(minutes=1)
        db.add(m)
    db.commit()
    a_players = [db.get(PickupRequest, m.a_request_id).player_id for m in pending]
    b_players = [db.get(PickupRequest, m.b_request_id).player_id for m in pending]
with Session(database.engine) as db:
    pickups.sweep(db)
    db.commit()
    for pa, pb in zip(a_players, b_players):
        ra = db.exec(select(PickupRequest).where(PickupRequest.player_id == pa).order_by(PickupRequest.id.desc())).first()
        rb = db.exec(select(PickupRequest).where(PickupRequest.player_id == pb).order_by(PickupRequest.id.desc())).first()
        check(f"unanswered: {NAMES[pb]} out, {NAMES[pa]} back in", rb.status == "declined" and ra.status in ("open", "proposed"), (ra.status, rb.status))

# ── cancelling a confirmed game ──
dee_req = mine(1)[0]["id"]
C[1].post(f"/pickups/{dee_req}/cancel", headers=H)
back = mine(partner_id)[0]
check("calling off a confirmed game puts the other back in the pool", back["status"] in ("open", "proposed"), back)
check("and it's posted as off", any("Pick-up game off" in p for p in POSTS))

# Where a game is, as players read it: "at the club" rather than the club's
# full name (which squeezed the day and time on Home); "place to be agreed"
# when nobody named a place.
with Session(database.engine) as db:
    def req(venue, text=None):
        return PickupRequest(club_id=1, system="x", player_id=1, player_name="x", window_start=datetime(2026, 1, 1),
                             window_end=datetime(2026, 1, 1), venue=venue, venue_text=text)
    check("club venue reads 'at the club'", pickups._where(db, req("club")) == "at the club", pickups._where(db, req("club")))
    check("named place reads 'at <place>'", pickups._where(db, req("other", "Waylands")) == "at Waylands")
    check("no place reads 'place to be agreed'", pickups._where(db, req("tbc")) == "place to be agreed")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("All passed.")
