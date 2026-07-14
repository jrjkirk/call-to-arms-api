"""One-off verification script for the signups.club_id dual-run (Phase 1,
table 6 of 10). Not part of the app; run manually against staging.

Exercises the three real Signup-creating write sites through FastAPI
TestClient, against the real staging DB, with only the auth dependency
overridden (in-memory fake user, no real User row touched):

    POST /signups            (submit_signup, create branch)
    POST /signups/prearranged (submit_prearranged, su_a + su_b)
    POST /admin/signups       (admin_signup_create)

Uses a far-future test week (01/01/2099) so it can never collide with a
real signup week. Deletes every row it creates at the end, in a `finally`,
so staging is left exactly as it started.

Run with: python verify_signups_club_id.py [--post-contract]
--post-contract also re-hits the existing-row/read endpoints that matter
after the NOT NULL contract step.
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Signup, Pairing, Club, Player, User
from main import app
import auth

TEST_WEEK = "01/01/2099"


def _fake_super_admin(linked_player_id: int, club_id: int) -> User:
    return User(
        id=999999,
        discord_id="test-verify-signups",
        discord_name="Verify Script",
        player_id=linked_player_id,
        is_super_admin=True,
        club_id=club_id,
    )


def main():
    post_contract = "--post-contract" in sys.argv

    with Session(engine) as db:
        players = db.exec(select(Player).where(Player.active == True).limit(2)).all()
        if len(players) < 2:
            print("Need at least 2 active players on staging to test prearranged.")
            sys.exit(1)
        player_a, player_b = players[0], players[1]
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
        print(f"Using players: {player_a.id}={player_a.name}, {player_b.id}={player_b.name}")
        print(f"Manchester club_id = {manchester_id}")

    fake_user = _fake_super_admin(player_a.id, manchester_id)
    app.dependency_overrides[auth.require_user] = lambda: fake_user
    app.dependency_overrides[auth.current_user] = lambda: fake_user

    client = TestClient(app)
    created_signup_ids = []
    created_pairing_ids = []
    problems = []

    try:
        # 1. POST /signups (submit_signup create branch)
        r = client.post("/signups", json={
            "system": "Kill Team",
            "week": TEST_WEEK,
            "faction": "Death Korps",
            "standby_ok": False,
        })
        print("POST /signups ->", r.status_code, r.json() if r.status_code != 500 else r.text)
        if r.status_code != 200:
            problems.append(f"POST /signups failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            su_id = body["signup"]["id"]
            created_signup_ids.append(su_id)
            with Session(engine) as db:
                su = db.get(Signup, su_id)
                print(f"  signup id={su.id} club_id={su.club_id}")
                if su.club_id != manchester_id:
                    problems.append(f"POST /signups: club_id={su.club_id}, expected {manchester_id}")

        # 1b. POST /signups again, same system/week/player -> update-existing
        # branch (not create). Confirms club_id survives an update and the
        # row id doesn't change.
        r = client.post("/signups", json={
            "system": "Kill Team",
            "week": TEST_WEEK,
            "faction": "Adeptus Custodes",
            "standby_ok": True,
        })
        print("POST /signups (update branch) ->", r.status_code, r.json() if r.status_code != 500 else r.text)
        if r.status_code != 200:
            problems.append(f"POST /signups (update branch) failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            if body["created"]:
                problems.append("POST /signups second call: expected created=False (update branch), got True")
            su_id_2 = body["signup"]["id"]
            if su_id_2 != su_id:
                problems.append(f"POST /signups (update branch): expected same id {su_id}, got {su_id_2}")
            with Session(engine) as db:
                su = db.get(Signup, su_id_2)
                print(f"  signup id={su.id} club_id={su.club_id} faction={su.faction}")
                if su.club_id != manchester_id:
                    problems.append(f"POST /signups update branch: club_id={su.club_id} (clobbered), expected {manchester_id}")
                if su.faction != "Adeptus Custodes":
                    problems.append("POST /signups update branch: faction not updated")

        # 2. POST /signups/prearranged (su_a, su_b)
        r = client.post("/signups/prearranged", json={
            "system": "The Old World",
            "week": TEST_WEEK,
            "player_a_id": player_a.id,
            "player_b_id": player_b.id,
            "faction_a": "Empire of Man",
            "faction_b": "Orc & Goblin Tribes",
        })
        print("POST /signups/prearranged ->", r.status_code, r.json() if r.status_code != 500 else r.text)
        if r.status_code != 200:
            problems.append(f"POST /signups/prearranged failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            su_a_id = body["signup_a"]["id"]
            su_b_id = body["signup_b"]["id"]
            pairing_id = body["pairing"]["id"]
            created_signup_ids += [su_a_id, su_b_id]
            created_pairing_ids.append(pairing_id)
            with Session(engine) as db:
                su_a = db.get(Signup, su_a_id)
                su_b = db.get(Signup, su_b_id)
                pairing = db.get(Pairing, pairing_id)
                print(f"  su_a id={su_a.id} club_id={su_a.club_id}")
                print(f"  su_b id={su_b.id} club_id={su_b.club_id}")
                print(f"  pairing id={pairing.id} club_id={getattr(pairing, 'club_id', 'N/A (no column yet)')}")
                if su_a.club_id != manchester_id:
                    problems.append(f"prearranged su_a: club_id={su_a.club_id}, expected {manchester_id}")
                if su_b.club_id != manchester_id:
                    problems.append(f"prearranged su_b: club_id={su_b.club_id}, expected {manchester_id}")

        # 3. POST /admin/signups (admin_signup_create) — needs a 3rd player or different system/week combo
        players_all = None
        with Session(engine) as db:
            players_all = db.exec(select(Player).where(Player.active == True).limit(3)).all()
        third_player = players_all[2] if len(players_all) >= 3 else players_all[0]
        # Use a different system so admin create doesn't 409 against step 1's KT signup
        r = client.post("/admin/signups", json={
            "system": "The Horus Heresy",
            "week": TEST_WEEK,
            "player_id": third_player.id,
            "faction": "I - Dark Angels",
        })
        print("POST /admin/signups ->", r.status_code, r.json() if r.status_code != 500 else r.text)
        if r.status_code != 201:
            problems.append(f"POST /admin/signups failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            su_id = body["id"]
            created_signup_ids.append(su_id)
            with Session(engine) as db:
                su = db.get(Signup, su_id)
                print(f"  signup id={su.id} club_id={su.club_id}")
                if su.club_id != manchester_id:
                    problems.append(f"POST /admin/signups: club_id={su.club_id}, expected {manchester_id}")

        if post_contract:
            # Re-hit read endpoints that matter post-contract
            r = client.get("/signups/mine", params={"system": "Kill Team", "week": TEST_WEEK})
            print("GET /signups/mine ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /signups/mine failed: {r.status_code} {r.text}")

            r = client.get("/signups/unpaired", params={"system": "Kill Team", "week": TEST_WEEK})
            print("GET /signups/unpaired ->", r.status_code)
            if r.status_code not in (200, 404, 422):
                problems.append(f"GET /signups/unpaired unexpected: {r.status_code} {r.text}")

            r = client.get("/signups/stats", params={"system": "Kill Team", "week": TEST_WEEK})
            print("GET /signups/stats ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /signups/stats failed: {r.status_code} {r.text}")

            # Dry-run only — exercises pairings_engine.generate()'s Signup reads,
            # no DB writes (persist=False).
            r = client.post("/admin/pairings/preview", json={"system": "Kill Team", "week": TEST_WEEK})
            print("POST /admin/pairings/preview ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"POST /admin/pairings/preview failed: {r.status_code} {r.text}")

    finally:
        with Session(engine) as db:
            for pid in created_pairing_ids:
                p = db.get(Pairing, pid)
                if p:
                    db.delete(p)
            for sid in created_signup_ids:
                su = db.get(Signup, sid)
                if su:
                    db.delete(su)
            db.commit()
            print(f"Cleaned up {len(created_signup_ids)} signup(s), {len(created_pairing_ids)} pairing(s).")

    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
