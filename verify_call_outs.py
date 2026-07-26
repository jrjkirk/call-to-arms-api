"""End-to-end verification for the ad-hoc call-out feature, against the real
staging DB via FastAPI TestClient (auth + active club overridden in-memory,
no real user/session touched). Exercises create → list → take → cancel and the
guard paths, then deletes every row it created so staging is left as it was.

Run:  PYTHONPATH=. python verify_call_outs.py
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import CallOut, User
from auth import require_user, active_club_id
import main

CLUB_ID = 1
SYSTEM = "The Old World"
USER1_ID, PLAYER1 = 1, "Joel Kirk"       # Kirkboi -> player 5
USER2_ID, PLAYER2 = 2, "Testy McTestface" # Testy   -> player 6

created_ids: list[int] = []
problems: list[str] = []


def as_user(uid: int):
    with Session(engine) as db:
        u = db.get(User, uid)
    main.app.dependency_overrides[require_user] = lambda: u
    main.app.dependency_overrides[active_club_id] = lambda: CLUB_ID


def check(cond: bool, msg: str):
    print(("  ok  " if cond else " FAIL ") + msg)
    if not cond:
        problems.append(msg)


def main_flow():
    client = TestClient(main.app)
    future_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")

    # 1. create as user 1
    as_user(USER1_ID)
    r = client.post("/call-outs", json={
        "system": SYSTEM, "location": "Element Games, Stockport",
        "game_date": future_date, "game_time": "18:30",
        "vibe": "Casual", "faction": "Empire of Man", "points": 2000,
        "notes": "verify script — safe to ignore",
    })
    check(r.status_code == 200, f"create -> 200 (got {r.status_code}: {r.text[:120]})")
    co = r.json()
    created_ids.append(co["id"])
    check(co["status"] == "open", "created call-out is open")
    check(co["is_mine"] is True, "creator sees is_mine=True")
    check(co["when_label"] and co["points"] == 2000, "when_label + points populated")

    # 2. list as user 1 -> present, is_mine True
    r = client.get("/call-outs", params={"system": SYSTEM})
    ids = [c["id"] for c in r.json()["call_outs"]]
    check(co["id"] in ids, "call-out appears in list")

    # 3. reject taking your own
    r = client.post(f"/call-outs/{co['id']}/take")
    check(r.status_code == 409, f"creator taking own -> 409 (got {r.status_code})")

    # 4. list as user 2 -> is_mine False, then take it
    as_user(USER2_ID)
    r = client.get("/call-outs", params={"system": SYSTEM})
    mine = next((c for c in r.json()["call_outs"] if c["id"] == co["id"]), None)
    check(mine is not None and mine["is_mine"] is False, "other player sees is_mine=False")
    r = client.post(f"/call-outs/{co['id']}/take")
    check(r.status_code == 200 and r.json()["status"] == "taken", f"take -> 200/taken (got {r.status_code})")

    # 5. taken one no longer in open list; can't take again
    r = client.get("/call-outs", params={"system": SYSTEM})
    check(co["id"] not in [c["id"] for c in r.json()["call_outs"]], "taken call-out drops out of open list")
    r = client.post(f"/call-outs/{co['id']}/take")
    check(r.status_code == 409, f"re-take a taken call-out -> 409 (got {r.status_code})")

    # 6. cancel flow: create as user1, non-creator can't cancel, creator can
    as_user(USER1_ID)
    r = client.post("/call-outs", json={
        "system": SYSTEM, "location": "Table 4",
        "game_date": future_date, "game_time": "12:00", "faction": "Orc & Goblin Tribes",
    })
    co2 = r.json()
    created_ids.append(co2["id"])
    as_user(USER2_ID)
    r = client.post(f"/call-outs/{co2['id']}/cancel")
    check(r.status_code == 403, f"non-creator cancel -> 403 (got {r.status_code})")
    as_user(USER1_ID)
    r = client.post(f"/call-outs/{co2['id']}/cancel")
    check(r.status_code == 200 and r.json()["status"] == "cancelled", f"creator cancel -> 200/cancelled (got {r.status_code})")

    # 7. past-date rejected at create
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    r = client.post("/call-outs", json={"system": SYSTEM, "location": "x", "game_date": past, "game_time": "18:30", "faction": "Empire of Man"})
    check(r.status_code == 422, f"past-dated create -> 422 (got {r.status_code})")


def cleanup():
    main.app.dependency_overrides.clear()
    with Session(engine) as db:
        for cid in created_ids:
            row = db.get(CallOut, cid)
            if row:
                db.delete(row)
        db.commit()
        remaining = db.exec(select(CallOut)).all()
    print(f"\nCleanup: deleted {len(created_ids)} test call-out(s); table now has {len(remaining)} row(s).")
    check(len(remaining) == 0, "staging call_outs back to 0 rows")


if __name__ == "__main__":
    try:
        main_flow()
    finally:
        cleanup()
    print("\n" + ("ALL CHECKS PASSED" if not problems else f"{len(problems)} PROBLEM(S): " + "; ".join(problems)))
