"""One-off verification script for the scoped-query-helper phase, chunk 4a
(signups.py part 1: my_signup, submit_signup, get_unpaired, _signup_count).
Not part of the app; run manually against staging.

Proves, beyond "no errors":
  1. GET /signups/mine ("current" and "last") is scoped by user.club_id —
     even a Signup row sharing the caller's real player_id but tagged with
     a DIFFERENT club_id (simulating bad/foreign data) is excluded from
     both "current" and "last", including when that foreign row has a
     higher id (so an unscoped "order by id desc" would have picked it).
  2. POST /signups upsert lookup ("existing") is scoped by user.club_id —
     a foreign-club row sharing the same week/system/player_id is not
     treated as the row to update, is not deleted as a "duplicate", and
     is left completely untouched. New rows get the real user.club_id
     (not the old _default_club_id() placeholder).
  3. GET /signups/unpaired is scoped by user.club_id on both the
     PublishState gate and the Pairing BYE lookup — a second club
     publishing pairings for the IDENTICAL week/system never appears in
     the caller's unpaired/BYE list, and the caller's own club's gate is
     evaluated independently (absent for the caller's club still yields
     [] even though the other club's gate is published=True for the same
     week/system key).
  4. _signup_count() is scoped by club_id — two clubs each with one
     signup for the identical week/system each count 1, not 2.

All rows this script creates (a genuine second temp club + its player,
temp Manchester player, temp Signup/Pairing/PublishState rows) are
cleaned up in a `finally`, leaving staging exactly as it started.

Run with: python verify_scoped_helper_chunk4a_signups.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, Signup, Pairing, PublishState, Player, User
from main import app
import auth
from signups import _signup_count

TEST_SYSTEM = "The Old World"
WEEK_MINE = "01/01/2099"
WEEK_POISONED = "02/01/2099"
WEEK_SUBMIT = "03/01/2099"
WEEK_UNPAIRED = "04/01/2099"
WEEK_COUNT = "05/01/2099"


def _fake_user(club_id: int, uid: int, player_id: int) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk4a-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=player_id,
        is_super_admin=False,
        club_id=club_id,
    )


def main():
    problems = []
    client = TestClient(app)

    with Session(engine) as db:
        manchester = db.exec(select(Club).where(Club.slug == "manchester")).first()
        manchester_id = manchester.id
    print(f"Manchester club_id = {manchester_id}")

    other_club_id = None
    created_player_ids = []
    created_signup_ids = []
    created_pairing_ids = []
    created_publish_ids = []

    try:
        # --- Second club + its own player, inserted directly ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk4a Other Club", slug="zztest-chunk4a-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            other_player = Player(name="ZZTest Chunk4a Other Player", club_id=other_club_id, active=True)
            db.add(other_player)
            db.commit()
            db.refresh(other_player)
            created_player_ids.append(other_player.id)
            other_player_id = other_player.id
        print(f"Created other club id={other_club_id} with player id={other_player_id}")

        # --- Manchester temp player for our caller ---
        with Session(engine) as db:
            m_player = Player(name="ZZTest Chunk4a Manchester Player", club_id=manchester_id, active=True)
            db.add(m_player)
            db.commit()
            db.refresh(m_player)
            created_player_ids.append(m_player.id)
            m_player_id = m_player.id
        print(f"Created Manchester player id={m_player_id}")

        manchester_caller = _fake_user(manchester_id, uid=999994, player_id=m_player_id)
        app.dependency_overrides[auth.current_user] = lambda: manchester_caller
        app.dependency_overrides[auth.require_user] = lambda: manchester_caller

        # =====================================================================
        # 1. GET /signups/mine — club_id scoping, including a same-player_id
        #    foreign-club row with a HIGHER id than the real row
        # =====================================================================
        resp = client.post("/signups", json={"system": TEST_SYSTEM, "week": WEEK_MINE, "faction": "Empire"})
        if resp.status_code != 200 or not resp.json().get("created"):
            problems.append(f"POST /signups (setup for my_signup test) failed: {resp.status_code} {resp.text}")
        real_signup_id = resp.json()["signup"]["id"]
        created_signup_ids.append(real_signup_id)
        print(f"Created real Manchester signup id={real_signup_id} for week={WEEK_MINE}")

        with Session(engine) as db:
            poisoned = Signup(
                week=WEEK_POISONED, system=TEST_SYSTEM,
                player_id=m_player_id, player_name="ZZTest Chunk4a Manchester Player",
                faction="Poisoned", club_id=other_club_id,
            )
            db.add(poisoned)
            db.commit()
            db.refresh(poisoned)
            created_signup_ids.append(poisoned.id)
            poisoned_id = poisoned.id
        print(f"Created poisoned Signup id={poisoned_id} (same player_id={m_player_id}, club_id={other_club_id}, higher id than real row: {poisoned_id > real_signup_id})")
        if poisoned_id <= real_signup_id:
            problems.append("Poisoned row id is not higher than the real row's id — 'last' test would be inconclusive")

        resp = client.get("/signups/mine", params={"system": TEST_SYSTEM, "week": WEEK_MINE})
        if resp.status_code != 200:
            problems.append(f"GET /signups/mine failed: {resp.status_code} {resp.text}")
        else:
            body = resp.json()
            if not body["current"] or body["current"]["id"] != real_signup_id:
                problems.append(f"GET /signups/mine 'current' expected id={real_signup_id}, got {body['current']}")
            else:
                print(f"GET /signups/mine 'current' -> id={body['current']['id']} (correct, real Manchester row)")
            if not body["last"] or body["last"]["id"] != real_signup_id:
                problems.append(f"GET /signups/mine 'last' expected id={real_signup_id} (poisoned row id={poisoned_id} must be excluded), got {body['last']}")
            else:
                print(f"GET /signups/mine 'last' -> id={body['last']['id']} (correct, poisoned foreign-club row with higher id excluded)")

        # =====================================================================
        # 2. POST /signups upsert scoping — foreign-club row with identical
        #    week/system/player_id must not be treated as 'existing'
        # =====================================================================
        with Session(engine) as db:
            poisoned_existing = Signup(
                week=WEEK_SUBMIT, system=TEST_SYSTEM,
                player_id=m_player_id, player_name="ZZTest Chunk4a Manchester Player",
                faction="ShouldNotBeTouched", club_id=other_club_id,
            )
            db.add(poisoned_existing)
            db.commit()
            db.refresh(poisoned_existing)
            created_signup_ids.append(poisoned_existing.id)
            poisoned_existing_id = poisoned_existing.id
        print(f"Created poisoned 'existing' Signup id={poisoned_existing_id} (club_id={other_club_id}) for week={WEEK_SUBMIT}")

        resp = client.post("/signups", json={"system": TEST_SYSTEM, "week": WEEK_SUBMIT, "faction": "Dwarfs"})
        if resp.status_code != 200:
            problems.append(f"POST /signups (create, poisoned-existing test) failed: {resp.status_code} {resp.text}")
        else:
            j = resp.json()
            if not j["created"]:
                problems.append("POST /signups expected created=True (foreign-club row must not count as 'existing'), got created=False")
            elif j["signup"]["club_id"] != manchester_id:
                problems.append(f"POST /signups created row club_id={j['signup']['club_id']}, expected {manchester_id}")
            else:
                created_signup_ids.append(j["signup"]["id"])
                manchester_submit_id = j["signup"]["id"]
                print(f"POST /signups (create) -> created=True, new row id={manchester_submit_id}, club_id={j['signup']['club_id']} (correct, real user.club_id)")

        with Session(engine) as db:
            still_there = db.get(Signup, poisoned_existing_id)
            if still_there is None or still_there.faction != "ShouldNotBeTouched":
                problems.append(f"Poisoned 'existing' row id={poisoned_existing_id} was modified or deleted by the scoped upsert")
            else:
                print(f"Poisoned row id={poisoned_existing_id} untouched (faction still 'ShouldNotBeTouched') after scoped create")

        resp = client.post("/signups", json={"system": TEST_SYSTEM, "week": WEEK_SUBMIT, "faction": "High Elves"})
        if resp.status_code != 200:
            problems.append(f"POST /signups (update path) failed: {resp.status_code} {resp.text}")
        else:
            j = resp.json()
            if j["created"] or j["signup"]["id"] != manchester_submit_id:
                problems.append(f"POST /signups (update) expected created=False on id={manchester_submit_id}, got {j}")
            else:
                print(f"POST /signups (update) -> created=False, updated own row id={j['signup']['id']} (correct)")

        with Session(engine) as db:
            still_there = db.get(Signup, poisoned_existing_id)
            if still_there is None or still_there.faction != "ShouldNotBeTouched":
                problems.append(f"Poisoned 'existing' row id={poisoned_existing_id} was modified or deleted by the scoped update")
            else:
                print(f"Poisoned row id={poisoned_existing_id} still untouched after scoped update")

        # =====================================================================
        # 3. GET /signups/unpaired — PublishState gate + Pairing BYE lookup
        #    scoped by club_id, even for an identical week/system key
        # =====================================================================
        with Session(engine) as db:
            other_bye_signup = Signup(
                week=WEEK_UNPAIRED, system=TEST_SYSTEM,
                player_id=other_player_id, player_name="ZZTest Chunk4a Other Player",
                club_id=other_club_id,
            )
            db.add(other_bye_signup)
            db.commit()
            db.refresh(other_bye_signup)
            other_bye_signup_id = other_bye_signup.id
            created_signup_ids.append(other_bye_signup_id)

            other_bye_pairing = Pairing(
                week=WEEK_UNPAIRED, system=TEST_SYSTEM,
                a_signup_id=other_bye_signup_id, b_signup_id=None,
                status="pending", prearranged=False,
                a_faction=None, b_faction=None,
                club_id=other_club_id,
            )
            db.add(other_bye_pairing)
            db.commit()
            db.refresh(other_bye_pairing)
            other_bye_pairing_id = other_bye_pairing.id
            created_pairing_ids.append(other_bye_pairing_id)

            other_publish = PublishState(week=WEEK_UNPAIRED, system=TEST_SYSTEM, published=True, club_id=other_club_id)
            db.add(other_publish)
            db.commit()
            db.refresh(other_publish)
            other_publish_id = other_publish.id
            created_publish_ids.append(other_publish_id)
        print(f"Created other club's published BYE (signup={other_bye_signup_id}, pairing={other_bye_pairing_id}, publish_state={other_publish_id}) for week={WEEK_UNPAIRED}")

        # No Manchester PublishState row exists yet for this week/system —
        # a leak would surface the other club's published BYE here.
        resp = client.get("/signups/unpaired", params={"system": TEST_SYSTEM, "week": WEEK_UNPAIRED})
        if resp.status_code != 200:
            problems.append(f"GET /signups/unpaired (no own gate) failed: {resp.status_code} {resp.text}")
        elif resp.json() != []:
            problems.append(f"GET /signups/unpaired (no own gate) expected [] , got {resp.json()} — leaked other club's BYE data")
        else:
            print("GET /signups/unpaired (Manchester has no gate row for this week/system) -> [] (correct, other club's published gate not used)")

        # Now Manchester also publishes for the identical week/system —
        # its own BYE list must appear, and the other club's must not.
        with Session(engine) as db:
            m_bye_signup = Signup(
                week=WEEK_UNPAIRED, system=TEST_SYSTEM,
                player_id=m_player_id, player_name="ZZTest Chunk4a Manchester Player",
                club_id=manchester_id,
            )
            db.add(m_bye_signup)
            db.commit()
            db.refresh(m_bye_signup)
            m_bye_signup_id = m_bye_signup.id
            created_signup_ids.append(m_bye_signup_id)

            m_bye_pairing = Pairing(
                week=WEEK_UNPAIRED, system=TEST_SYSTEM,
                a_signup_id=m_bye_signup_id, b_signup_id=None,
                status="pending", prearranged=False,
                a_faction=None, b_faction=None,
                club_id=manchester_id,
            )
            db.add(m_bye_pairing)
            db.commit()
            db.refresh(m_bye_pairing)
            m_bye_pairing_id = m_bye_pairing.id
            created_pairing_ids.append(m_bye_pairing_id)

            m_publish = PublishState(week=WEEK_UNPAIRED, system=TEST_SYSTEM, published=True, club_id=manchester_id)
            db.add(m_publish)
            db.commit()
            db.refresh(m_publish)
            m_publish_id = m_publish.id
            created_publish_ids.append(m_publish_id)
        print(f"Created Manchester's own published BYE (signup={m_bye_signup_id}, pairing={m_bye_pairing_id}, publish_state={m_publish_id}) for the SAME week/system")

        resp = client.get("/signups/unpaired", params={"system": TEST_SYSTEM, "week": WEEK_UNPAIRED})
        if resp.status_code != 200:
            problems.append(f"GET /signups/unpaired (both clubs published) failed: {resp.status_code} {resp.text}")
        else:
            names = {row["player_name"] for row in resp.json()}
            print(f"GET /signups/unpaired (both clubs published, same week/system) -> {names}")
            if "ZZTest Chunk4a Other Player" in names:
                problems.append("GET /signups/unpaired leaked the other club's BYE player")
            if "ZZTest Chunk4a Manchester Player" not in names:
                problems.append("GET /signups/unpaired missing the caller's own club's BYE player")

        # =====================================================================
        # 4. _signup_count() scoping
        # =====================================================================
        with Session(engine) as db:
            other_count_signup = Signup(
                week=WEEK_COUNT, system=TEST_SYSTEM,
                player_id=other_player_id, player_name="ZZTest Chunk4a Other Player",
                club_id=other_club_id,
            )
            m_count_signup = Signup(
                week=WEEK_COUNT, system=TEST_SYSTEM,
                player_id=m_player_id, player_name="ZZTest Chunk4a Manchester Player",
                club_id=manchester_id,
            )
            db.add(other_count_signup)
            db.add(m_count_signup)
            db.commit()
            db.refresh(other_count_signup)
            db.refresh(m_count_signup)
            created_signup_ids.extend([other_count_signup.id, m_count_signup.id])

        with Session(engine) as db:
            manchester_count = _signup_count(db, TEST_SYSTEM, WEEK_COUNT, manchester_id)
            other_count = _signup_count(db, TEST_SYSTEM, WEEK_COUNT, other_club_id)
        if manchester_count != 1:
            problems.append(f"_signup_count(manchester_id) expected 1, got {manchester_count}")
        else:
            print(f"_signup_count(club_id=manchester_id) -> 1 (correct, other club's identically-shaped signup excluded)")
        if other_count != 1:
            problems.append(f"_signup_count(other_club_id) expected 1, got {other_count}")
        else:
            print(f"_signup_count(club_id=other_club_id) -> 1 (correct, isolated the other way too)")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for pid in created_pairing_ids:
                p = db.get(Pairing, pid)
                if p:
                    db.delete(p)
            db.commit()
            for sid in created_signup_ids:
                s = db.get(Signup, sid)
                if s:
                    db.delete(s)
            db.commit()
            for pubid in created_publish_ids:
                pub = db.get(PublishState, pubid)
                if pub:
                    db.delete(pub)
            db.commit()
            for pid in created_player_ids:
                p = db.get(Player, pid)
                if p:
                    db.delete(p)
            db.commit()
            if other_club_id:
                club = db.get(Club, other_club_id)
                if club:
                    db.delete(club)
                    db.commit()

            final_clubs = db.exec(select(Club)).all()
            print(f"Cleanup done: clubs={len(final_clubs)} (expect 1, Manchester only)")

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print(
            "\nVerification passed: GET /signups/mine and POST /signups are "
            "scoped by user.club_id (a foreign-club row sharing the same "
            "player_id is excluded/untouched even with a higher id), "
            "GET /signups/unpaired never leaks another club's published "
            "BYE data for an identical week/system, and _signup_count() "
            "counts only the requested club's signups."
        )


if __name__ == "__main__":
    main()
