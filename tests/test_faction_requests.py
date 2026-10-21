"""Faction requests: a system admin asks, the platform admin approves.

A system's faction list is shared by every club, so the only way a club admin
changes it is by asking. What has to hold:
  - only an admin of THAT system can ask, and names already listed or already
    waiting are skipped, not duplicated;
  - approving appends exactly the approved name, with the spelling fixed if the
    reviewer fixed it, and never lists a faction twice;
  - a system with categories refuses an approval with no category;
  - a bad decision applies nothing, not half the batch;
  - the alerts channel hears about a request and pings only the platform
    admins; the updates channel hears about an approval; the requester gets a
    notice. A name typed as "@everyone" pings nobody.

Run: PYTHONPATH=. python tests/test_faction_requests.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "faction_requests.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"
os.environ["ALERTS_WEBHOOK_URL"] = "https://alerts.example/hook"
os.environ["DISCORD_UPDATES_WEBHOOK_URL"] = "https://updates.example/hook"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import faction_requests  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    AdminRole, Club, ClubSystem, FactionRequest, PlayerNotice, SystemConfig, User, UserIdentity,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


POSTS: list[tuple[str, dict]] = []


class _Resp:
    status_code = 204


def _capture(url, json=None, **_):
    POSTS.append((url, json))
    return _Resp()


faction_requests.httpx.post = _capture

KT = "Kill Team"
ME = "Middle Earth"

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=KT, slug="kt", legacy_system_name=KT, active=True,
                        vibe_options=["Casual"], default_vibe="Casual",
                        faction_list=["Kasrkin", "Legionaries"], icon_folder="KT"))
    db.add(SystemConfig(id=2, name=ME, slug="mesbg", legacy_system_name=ME, active=True,
                        vibe_options=["Casual"], default_vibe="Casual",
                        faction_list=["Rohan", "Mordor"],
                        faction_groups=[{"label": "Good", "factions": ["Rohan"]},
                                        {"label": "Evil", "factions": ["Mordor"]}],
                        icon_folder="MESBG"))
    for sid in (1, 2):
        db.add(ClubSystem(id=sid, club_id=1, system_id=sid, enabled=True,
                               session_day="Thursday", session_cadence="weekly"))
    db.add(User(id=1, discord_id="900", discord_name="Joel", club_id=1, home_club_id=1,
                is_platform_admin=True))
    db.add(UserIdentity(user_id=1, provider="discord", provider_user_id="900", is_primary=True))
    db.add(User(id=2, discord_id="901", discord_name="Oliver", club_id=1, home_club_id=1))
    db.add(AdminRole(user_id=2, scope=KT, club_id=1))
    db.add(User(id=3, discord_id="902", discord_name="Player", club_id=1, home_club_id=1))
    db.commit()


def client_for(uid):
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(uid))
    return c


joel, oliver, player = client_for(1), client_for(2), client_for(3)


print("\n1. Asking")
r = player.post("/admin/faction-requests", json={"system": KT, "names": ["Ratlings"]})
check("a non-admin cannot ask", r.status_code == 403, str(r.status_code))
r = oliver.post("/admin/faction-requests", json={"system": ME, "names": ["Gondor"]})
check("nor an admin of a different system", r.status_code == 403, str(r.status_code))

POSTS.clear()
r = oliver.post("/admin/faction-requests", json={
    "system": KT,
    "names": ["Ratlings", "  Wolf   Scouts ", "kasrkin", "Ratlings", "", "@everyone Hello"],
    "note": "New season",
})
check("the KT admin can ask", r.status_code == 200, r.text[:200])
body = r.json()
check("blanks and repeats dropped, spacing tidied",
      body["requested"] == ["Ratlings", "Wolf Scouts", "@everyone Hello"], str(body["requested"]))
check("a faction already listed is skipped, case-blind",
      body["skipped"] == [{"name": "kasrkin", "reason": "already in the list"}], str(body["skipped"]))

alerts = [p for u, p in POSTS if u == os.environ["ALERTS_WEBHOOK_URL"]]
check("the alerts channel is told once", len(alerts) == 1, str(POSTS))
check("and it pings the platform admin", alerts and "<@900>" in alerts[0]["content"])
check("only the platform admin can be pinged",
      alerts and alerts[0]["allowed_mentions"] == {"parse": [], "users": ["900"]},
      str(alerts and alerts[0]["allowed_mentions"]))

r = oliver.post("/admin/faction-requests", json={"system": KT, "names": ["ratlings"]})
check("asking again for a pending one is refused", r.status_code == 422, r.text[:160])

r = oliver.get(f"/admin/faction-requests?system={KT}")
check("the club sees its own requests", r.status_code == 200 and len(r.json()["requests"]) == 3,
      r.text[:160])


print("\n2. The queue is the platform admin's alone")
check("Oliver cannot read it", oliver.get("/admin/platform/faction-requests").status_code == 403)
check("nor review", oliver.post("/admin/platform/faction-requests/review",
                                json={"decisions": []}).status_code == 403)
q = joel.get("/admin/platform/faction-requests").json()
ids = {x["name"]: x["id"] for x in q["requests"]}
check("Joel sees all three pending", len(q["requests"]) == 3, str(q))
check("with the club and the icon filename",
      all(x["club_name"] == "EG NWGC" for x in q["requests"])
      and next(x for x in q["requests"] if x["name"] == "Wolf Scouts")["icon_file"] == "wolf_scouts.png")


print("\n3. A bad decision applies nothing")
r = joel.post("/admin/platform/faction-requests/review", json={"decisions": [
    {"id": ids["Ratlings"], "action": "approve"},
    {"id": ids["Wolf Scouts"], "action": "maybe"},
]})
check("refused", r.status_code == 422, r.text[:160])
with Session(database.engine) as db:
    check("the list is untouched", db.get(SystemConfig, 1).faction_list == ["Kasrkin", "Legionaries"])
    check("and nothing was marked reviewed",
          all(x.status == "pending" for x in db.exec(select(FactionRequest)).all()))


print("\n4. Approving and declining together")
POSTS.clear()
r = joel.post("/admin/platform/faction-requests/review", json={"decisions": [
    {"id": ids["Ratlings"], "action": "approve"},
    {"id": ids["Wolf Scouts"], "action": "approve", "name": "Wolf Scouts Pack"},
    {"id": ids["@everyone Hello"], "action": "decline", "reason": "Not a real team"},
]})
check("accepted", r.status_code == 200, r.text[:200])
with Session(database.engine) as db:
    kt = db.get(SystemConfig, 1)
    check("approved names appended in order, with the fix applied",
          kt.faction_list == ["Kasrkin", "Legionaries", "Ratlings", "Wolf Scouts Pack"],
          str(kt.faction_list))
    rows = {x.name: x for x in db.exec(select(FactionRequest)).all()}
    check("the fixed name is recorded", rows["Wolf Scouts"].approved_name == "Wolf Scouts Pack")
    check("the decline and its reason are recorded",
          rows["@everyone Hello"].status == "declined"
          and rows["@everyone Hello"].decline_reason == "Not a real team")
    notices = db.exec(select(PlayerNotice).where(PlayerNotice.user_id == 2)).all()
    check("Oliver gets ONE notice covering the batch", len(notices) == 1, str(len(notices)))
    check("saying what was added and what wasn't",
          notices and "Ratlings, Wolf Scouts Pack" in notices[0].body
          and "Not added: @everyone Hello (Not a real team)" in notices[0].body,
          notices and notices[0].body)

updates = [p for u, p in POSTS if u == os.environ["DISCORD_UPDATES_WEBHOOK_URL"]]
check("the updates channel gets one post", len(updates) == 1, str(POSTS))
check("naming only the added factions",
      updates and "Ratlings, Wolf Scouts Pack" in updates[0]["content"]
      and "everyone" not in updates[0]["content"], updates and updates[0]["content"])
check("and it can ping nobody",
      updates and updates[0]["allowed_mentions"] == {"parse": [], "users": []})

r = joel.post("/admin/platform/faction-requests/review",
              json={"decisions": [{"id": ids["Ratlings"], "action": "decline"}]})
check("a reviewed request cannot be reviewed again", r.status_code == 409, r.text[:120])

systems = {s["legacy_system_name"]: s for s in joel.get("/systems").json()}
check("the signup catalogue serves the new teams at once",
      "Ratlings" in (systems[KT]["faction_list"] or []), str(systems[KT]["faction_list"]))


print("\n5. A system with categories")
with Session(database.engine) as db:
    db.add(AdminRole(user_id=2, scope=ME, club_id=1))
    db.commit()
r = oliver.post("/admin/faction-requests", json={
    "system": ME, "names": ["Gondor", "Isengard"], "suggested_group": "Good"})
check("asked", r.status_code == 200, r.text[:160])
q = joel.get("/admin/platform/faction-requests").json()
me_ids = {x["name"]: x["id"] for x in q["requests"] if x["system"] == ME}
check("the queue carries the categories to choose from",
      q["systems"][ME]["groups"] == ["Good", "Evil"], str(q["systems"]))
check("and the requester's suggestion",
      all(x["suggested_group"] == "Good" for x in q["requests"] if x["system"] == ME))

r = joel.post("/admin/platform/faction-requests/review",
              json={"decisions": [{"id": me_ids["Gondor"], "action": "approve"}]})
check("approving with no category is refused", r.status_code == 422, r.text[:160])

r = joel.post("/admin/platform/faction-requests/review", json={"decisions": [
    {"id": me_ids["Gondor"], "action": "approve", "group": "Good"},
    {"id": me_ids["Isengard"], "action": "approve", "group": "Evil"},
]})
check("with categories it goes through", r.status_code == 200, r.text[:160])
with Session(database.engine) as db:
    me = db.get(SystemConfig, 2)
    groups = {g["label"]: g["factions"] for g in me.faction_groups}
    check("each lands in the category chosen, not the one suggested",
          groups == {"Good": ["Rohan", "Gondor"], "Evil": ["Mordor", "Isengard"]}, str(groups))
    check("and in the flat list", me.faction_list == ["Rohan", "Mordor", "Gondor", "Isengard"],
          str(me.faction_list))


print("\n6. Two requests for one faction never list it twice")
with Session(database.engine) as db:
    kt = db.get(SystemConfig, 1)
    kt.faction_list = kt.faction_list + ["Hand Of The Archon"]  # added by hand meanwhile
    db.add(kt)
    db.add(FactionRequest(batch_id="x", club_id=1, system=KT, name="hand of the archon",
                          requested_by_user_id=2, requested_by_name="Oliver"))
    db.commit()
    rid = db.exec(select(FactionRequest).where(FactionRequest.batch_id == "x")).first().id
r = joel.post("/admin/platform/faction-requests/review",
              json={"decisions": [{"id": rid, "action": "approve"}]})
check("approved", r.status_code == 200, r.text[:160])
with Session(database.engine) as db:
    fl = db.get(SystemConfig, 1).faction_list
    check("listed once, with the existing spelling",
          [f for f in fl if f.lower() == "hand of the archon"] == ["Hand Of The Archon"], str(fl))
    check("and the request records that spelling",
          db.get(FactionRequest, rid).approved_name == "Hand Of The Archon")



print("\n7. An alphabetical system slots approvals into place")
with Session(database.engine) as db:
    kt = db.get(SystemConfig, 1)
    kt.faction_order = "alphabetical"
    kt.faction_list = sorted(kt.faction_list) + ["Unlisted Kill Team"]
    db.add(kt)
    db.add(FactionRequest(batch_id="y", club_id=1, system=KT, name="Blooded",
                          requested_by_user_id=2, requested_by_name="Oliver"))
    db.commit()
    rid = db.exec(select(FactionRequest).where(FactionRequest.batch_id == "y")).first().id
r = joel.post("/admin/platform/faction-requests/review",
              json={"decisions": [{"id": rid, "action": "approve"}]})
check("approved", r.status_code == 200, r.text[:160])
with Session(database.engine) as db:
    fl = db.get(SystemConfig, 1).faction_list
    check("it lands in alphabetical order, not at the end",
          fl[:-1] == sorted(fl[:-1], key=str.casefold) and "Blooded" in fl
          and fl[-1] == "Unlisted Kill Team", str(fl))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
