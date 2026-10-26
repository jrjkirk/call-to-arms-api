"""Push notifications (push.py): the notices, sent to a player's devices.

What must hold:
  - push is off (and says so) until the VAPID keys are set
  - a device subscribes, reports its status, mutes kinds, unsubscribes
  - a device signed into another account moves to that account
  - a notice written in a transaction is pushed only once it COMMITS, and a
    rolled-back one is never pushed
  - a muted kind isn't sent to that device, but still is to the others
  - an address the push service calls gone (410) is deleted
  - nobody can read or change another account's device

Run: PYTHONPATH=. python tests/test_push.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime

_DB = pathlib.Path(tempfile.mkdtemp()) / "push.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"
os.environ.pop("VAPID_PUBLIC_KEY", None)
os.environ.pop("VAPID_PRIVATE_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import notices  # noqa: E402
import push  # noqa: E402
from main import app  # noqa: E402
from models import Club, ClubSystem, Player, PushSubscription, Signup, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW, WEEK = "The Old World", "28/10/2026"
H = {"origin": "https://egnwgc.calltoarms.app"}
push.SEND_IN_BACKGROUND = False
SENT: list[tuple[str, dict]] = []
GONE: set[str] = set()


def fake_send(sub, payload):
    if sub.endpoint in GONE:
        return 410
    SENT.append((sub.endpoint, payload))
    return None


push._send_one = fake_send

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly"))
    for uid, name in [(1, "Dee"), (2, "Gail"), (3, "Pat")]:
        db.add(User(id=uid, display_name=name, club_id=1, home_club_id=1))
        db.add(Player(id=uid, name=name, club_id=1, user_id=uid, active=True))
        db.add(Signup(id=uid, week=WEEK, system=TOW, player_id=uid, player_name=name, club_id=1,
                      created_at=datetime(2026, 10, 1, 12, uid)))
    db.commit()


def client_for(uid):
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(uid))
    return c


def sub_body(endpoint):
    return {"endpoint": endpoint, "keys": {"p256dh": "BPk", "auth": "au"}}


dee, gail = client_for(1), client_for(2)

# ── off until keys are set ──
r = TestClient(app).get("/push/key").json()
check("off without keys", r["enabled"] is False and r["public_key"] is None, r)
check("subscribe refused while off", dee.post("/push/subscribe", json=sub_body("https://push.example/a"), headers=H).status_code == 503)

os.environ["VAPID_PUBLIC_KEY"] = "BPublicKeyForTests"
os.environ["VAPID_PRIVATE_KEY"] = "privatekeyfortests"
r = TestClient(app).get("/push/key").json()
check("on with keys, and lists the kinds", r["enabled"] and r["public_key"] == "BPublicKeyForTests" and len(r["kinds"]) == 4, r)
check("faction requests aren't a switch", "faction_request" not in {k["kind"] for k in r["kinds"]}, r["kinds"])

# A kind with no switch stays an in-app notice: nothing is queued to push.
import push as _push  # noqa: E402
with Session(database.engine) as _db:
    _push.queue(_db, user_id=1, kind="faction_request", body="Added.")
    _push.queue(_db, user_id=1, kind="pairings_published", body="Up.")
    queued = [i["kind"] for i in _db.info.get("pending_push", [])]
    _db.info.pop("pending_push", None)
check("only switchable kinds are pushed", queued == ["pairings_published"], queued)

# ── subscribe / status / mute / unsubscribe ──
check("subscribe", dee.post("/push/subscribe", json=sub_body("https://push.example/dee-phone"), headers=H).json()["subscribed"])
dee.post("/push/subscribe", json=sub_body("https://push.example/dee-laptop"), headers=H)
gail.post("/push/subscribe", json=sub_body("https://push.example/gail-phone"), headers=H)
check("status of own device", dee.post("/push/status", json={"endpoint": "https://push.example/dee-phone"}, headers=H).json()["subscribed"])
check("someone else's device reads as not subscribed",
      gail.post("/push/status", json={"endpoint": "https://push.example/dee-phone"}, headers=H).json()["subscribed"] is False)
check("can't mute someone else's device",
      gail.post("/push/mute", json={"endpoint": "https://push.example/dee-phone", "muted": ["pairings_published"]}, headers=H).status_code == 404)
r = dee.post("/push/mute", json={"endpoint": "https://push.example/dee-laptop", "muted": ["pairings_published", "nonsense"]}, headers=H).json()
check("mute keeps only real kinds", r["muted"] == ["pairings_published"], r)

# ── notices push after commit, respect mutes ──
SENT.clear()
with Session(database.engine) as db:
    notices.notify_pairings_published(db, 1, TOW, WEEK)
    check("nothing sent before commit", SENT == [], SENT)
    db.commit()
endpoints = sorted(e for e, _ in SENT)
check("sent to every unmuted device of everyone playing",
      endpoints == ["https://push.example/dee-phone", "https://push.example/gail-phone"], endpoints)
payload = SENT[0][1]
check("payload: title, the notice's words, and a link to that night's pairings",
      payload["title"] == "Pairings are up" and "pairings for" in payload["body"]
      and payload["url"].startswith("/pairings?") and "28%2F10%2F2026" in payload["url"], payload)

SENT.clear()
with Session(database.engine) as db:
    notices.record(db, user_id=3, club_id=1, system=TOW, week=WEEK, kind="game_found", body="x")
    db.rollback()
check("a rolled-back notice is never pushed", SENT == [], SENT)

# ── dead addresses are removed ──
GONE.add("https://push.example/gail-phone")
SENT.clear()
with Session(database.engine) as db:
    notices.record(db, user_id=2, club_id=1, system=TOW, week=WEEK, kind="opponent_dropped", body="y")
    db.commit()
with Session(database.engine) as db:
    left = db.exec(select(PushSubscription).where(PushSubscription.endpoint == "https://push.example/gail-phone")).first()
check("a 410 deletes the device", left is None)

# ── the device follows the account using it ──
client_for(3).post("/push/subscribe", json=sub_body("https://push.example/dee-phone"), headers=H)
with Session(database.engine) as db:
    row = db.exec(select(PushSubscription).where(PushSubscription.endpoint == "https://push.example/dee-phone")).first()
check("re-subscribing from another account moves the device", row is not None and row.user_id == 3, row)

# ── test push and unsubscribe ──
SENT.clear()
r = dee.post("/push/test", headers=H).json()
check("test push reaches the account's remaining device", r["delivered"] == 1 and SENT[0][0] == "https://push.example/dee-laptop", (r, SENT))
dee.post("/push/unsubscribe", json={"endpoint": "https://push.example/dee-laptop"}, headers=H)
check("unsubscribe", dee.post("/push/status", json={"endpoint": "https://push.example/dee-laptop"}, headers=H).json()["subscribed"] is False)
check("signed out is 401", TestClient(app).post("/push/status", json={"endpoint": "x"}).status_code == 401)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("All passed.")
