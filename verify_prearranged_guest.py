"""End-to-end verification for the pre-arranged "+1 / guest" feature, against
the real staging DB via FastAPI TestClient (auth + active club overridden
in-memory, no real user/session touched).

Proves a guest game (member vs a +1 with no profile):
  - creates a Signup with player_id NULL + the typed name,
  - links a prearranged Pairing,
  - surfaces on the public /pairings cards (name shown, id null, not a BYE),
  - regresses cleanly against a normal two-member prearranged game,
  - rejects a blank guest name.
Uses a far-future test week so it can never collide with real signups, and
deletes every row it creates so staging is left exactly as it was.

Run:  PYTHONPATH=. python verify_prearranged_guest.py
"""
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Signup, Pairing, PublishState, User
from auth import require_user, active_club_id
import main

CLUB_ID = 1
SYSTEM = "The Old World"
WEEK = "01/01/2099"            # far future — cannot collide with real data
WEEK2 = "08/01/2099"           # separate week for the member-vs-member regression
JOEL_PID = 5                    # real active staging player, club 1
TESTY_PID = 6                   # real active staging player, club 1
FAC_A = "Empire of Man"
FAC_B = "Orc & Goblin Tribes"

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
    as_user(1)

    # 1. Guest game: Joel vs a +1 with no profile.
    r = client.post("/signups/prearranged", json={
        "system": SYSTEM, "week": WEEK,
        "player_a_id": JOEL_PID, "guest_b_name": "  Verify Guest  ",
        "faction_a": FAC_A, "faction_b": FAC_B,
        "eta": "18:30", "vibe": "Casual", "points": 2000,
    })
    check(r.status_code == 200, f"guest prearranged -> 200 (got {r.status_code}: {r.text[:160]})")
    body = r.json()
    su_b = body["signup_b"]
    check(su_b["player_id"] is None, "guest signup has player_id NULL")
    check(su_b["player_name"] == "Verify Guest", f"guest name stored trimmed (got {su_b['player_name']!r})")
    check(body["signup_a"]["player_id"] == JOEL_PID, "member signup keeps real player_id")
    check(body["pairing"]["prearranged"] is True, "pairing is marked prearranged")

    # 2. Blank guest name rejected.
    r = client.post("/signups/prearranged", json={
        "system": SYSTEM, "week": WEEK,
        "player_a_id": JOEL_PID, "guest_b_name": "   ",
        "faction_a": FAC_A, "faction_b": FAC_B,
    })
    check(r.status_code == 422, f"blank guest name -> 422 (got {r.status_code})")

    # 3. Guest shows up on the public /pairings cards (publish the gate first).
    with Session(engine) as db:
        db.add(PublishState(week=WEEK, system=SYSTEM, published=True, club_id=CLUB_ID))
        db.commit()
    r = client.get("/pairings", params={"system": SYSTEM, "week": WEEK})
    check(r.status_code == 200, f"/pairings -> 200 (got {r.status_code})")
    matchups = r.json().get("matchups", [])
    guest_card = next((m for m in matchups if m.get("player_b_name") == "Verify Guest"), None)
    check(guest_card is not None, "guest matchup present on /pairings cards")
    if guest_card:
        check(guest_card["player_a_name"] == "Joel Kirk", "card shows the member as player A")
        check(guest_card["player_b_id"] is None, "guest side has null player_b_id (identifies the +1)")
        check(guest_card["is_bye"] is False, "guest matchup is NOT a BYE")
        check(guest_card["player_b_faction"] == FAC_B, "guest faction shown on card")

    # 4. Regression: a normal two-member prearranged game still works (own week
    #    so Joel isn't caught by the correct "already signed up" guard above).
    r = client.post("/signups/prearranged", json={
        "system": SYSTEM, "week": WEEK2,
        "player_a_id": JOEL_PID, "player_b_id": TESTY_PID,
        "faction_a": FAC_A, "faction_b": FAC_B,
        "eta": "18:30", "vibe": "Casual", "points": 2000,
    })
    check(r.status_code == 200, f"two-member prearranged -> 200 (got {r.status_code}: {r.text[:160]})")
    if r.status_code == 200:
        rb = r.json()
        check(rb["signup_b"]["player_id"] == TESTY_PID, "member-vs-member still stores real player_id B")


def cleanup():
    main.app.dependency_overrides.clear()
    weeks = (WEEK, WEEK2)
    with Session(engine) as db:
        prs = db.exec(select(Pairing).where(Pairing.week.in_(weeks)).where(Pairing.system == SYSTEM)).all()
        for p in prs:
            db.delete(p)
        sus = db.exec(select(Signup).where(Signup.week.in_(weeks)).where(Signup.system == SYSTEM)).all()
        for s in sus:
            db.delete(s)
        gates = db.exec(select(PublishState).where(PublishState.week.in_(weeks)).where(PublishState.system == SYSTEM)).all()
        for g in gates:
            db.delete(g)
        db.commit()
        left_p = db.exec(select(Pairing).where(Pairing.week.in_(weeks))).all()
        left_s = db.exec(select(Signup).where(Signup.week.in_(weeks))).all()
    print(f"\nCleanup: removed {len(prs)} pairing(s), {len(sus)} signup(s), {len(gates)} publish gate(s).")
    check(not left_p and not left_s, "test week left with 0 pairings + 0 signups")


if __name__ == "__main__":
    try:
        main_flow()
    finally:
        cleanup()
    print("\n" + ("ALL CHECKS PASSED" if not problems else f"{len(problems)} PROBLEM(S): " + "; ".join(problems)))
