"""One-off verification script for the scoped-query-helper chunk 2
(auth.py's club-owned queries: me()'s claim_candidates, claim_player()'s
ownership check, create_profile()'s club_id). Not part of the app; run
manually against staging.

Proves, beyond "no errors":
  1. GET /auth/me's claim_candidates never offers a player belonging to a
     different club — a genuine second temporary club/player, inserted
     directly bypassing the app, must not appear for a Manchester caller.
  2. POST /auth/claim/{player_id} rejects (404, not a successful claim)
     an attempt to claim a player belonging to a different club, even
     when player_id is a real, active player id — just not the caller's
     club's.
  3. POST /auth/claim/{player_id} still succeeds normally for a player
     that IS in the caller's own club.
  4. POST /auth/create-profile writes club_id from the real
     user.club_id, not the old _default_club_id() placeholder.

All rows this script creates (the temp second club + its player, temp
Manchester players, temp Users) are cleaned up in a `finally`, leaving
staging exactly as it started.

Run with: python verify_scoped_helper_chunk2_auth.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, Player, User
from main import app
import auth


def _fake_user(club_id: int, uid: int, player_id=None) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-scoped-chunk2-{uid}",
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
    created_user_ids = []
    created_player_ids = []

    try:
        # --- Set up a second club + a player belonging to it, inserted
        # directly (bypassing the app), to prove scoping actually excludes it. ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk2 Other Club", slug="zztest-chunk2-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            other_player = Player(name="ZZTest Chunk2 Other Club Player", club_id=other_club_id, active=True)
            db.add(other_player)
            db.commit()
            db.refresh(other_player)
            created_player_ids.append(other_player.id)
            other_player_id = other_player.id
        print(f"Created other club id={other_club_id} with player id={other_player_id}")

        # --- A real, unclaimed Manchester player for the "successful claim
        # within the caller's own club" test ---
        with Session(engine) as db:
            manchester_temp_player = Player(name="ZZTest Chunk2 Manchester Player", club_id=manchester_id, active=True)
            db.add(manchester_temp_player)
            db.commit()
            db.refresh(manchester_temp_player)
            created_player_ids.append(manchester_temp_player.id)
            manchester_temp_player_id = manchester_temp_player.id
        print(f"Created Manchester player id={manchester_temp_player_id}")

        # =====================================================================
        # Test group A: temp_user_a — no linked player yet. Used for /auth/me
        # (candidate leak check) and /auth/claim (both cross-club and
        # same-club).
        # =====================================================================
        temp_user_a = _fake_user(manchester_id, uid=999995, player_id=None)
        app.dependency_overrides[auth.current_user] = lambda: temp_user_a
        app.dependency_overrides[auth.require_user] = lambda: temp_user_a

        # --- 1. GET /auth/me: claim_candidates must not leak the other club's player ---
        resp = client.get("/auth/me")
        if resp.status_code != 200:
            problems.append(f"GET /auth/me (temp_user_a) failed: {resp.status_code} {resp.text}")
        else:
            body = resp.json()
            candidate_ids = {c["id"] for c in body["claim_candidates"]}
            print(f"GET /auth/me (temp_user_a, no linked player) candidate_ids={candidate_ids}")
            if other_player_id in candidate_ids:
                problems.append(
                    f"GET /auth/me leaked other club's player (id={other_player_id}) into claim_candidates — club scoping not working!"
                )
            if manchester_temp_player_id not in candidate_ids:
                problems.append(
                    f"GET /auth/me did not offer the caller's own club's player (id={manchester_temp_player_id}) as a candidate"
                )

        # --- 2. POST /auth/claim/{other_player_id}: must be rejected (404), not claimed ---
        resp = client.post(f"/auth/claim/{other_player_id}")
        if resp.status_code != 404:
            problems.append(
                f"POST /auth/claim/{other_player_id} (cross-club) expected 404, got {resp.status_code} {resp.text}"
            )
        else:
            print(f"POST /auth/claim/{other_player_id} (cross-club) -> 404 (correct, rejected)")

        # --- 3. POST /auth/claim/{manchester_temp_player_id}: same-club claim must succeed ---
        # claim_player() needs a real, persisted User row to update (user.player_id = ...,
        # db.add(user), db.commit()) — persist temp_user_a for real now.
        with Session(engine) as db:
            persisted_user_a = User(
                discord_id=temp_user_a.discord_id,
                discord_name=temp_user_a.discord_name,
                player_id=None,
                is_super_admin=False,
                club_id=manchester_id,
            )
            db.add(persisted_user_a)
            db.commit()
            db.refresh(persisted_user_a)
            created_user_ids.append(persisted_user_a.id)

        app.dependency_overrides[auth.current_user] = lambda: persisted_user_a
        app.dependency_overrides[auth.require_user] = lambda: persisted_user_a

        # Re-run the cross-club rejection with the persisted user too, to be thorough.
        resp = client.post(f"/auth/claim/{other_player_id}")
        if resp.status_code != 404:
            problems.append(
                f"POST /auth/claim/{other_player_id} (cross-club, persisted user) expected 404, got {resp.status_code} {resp.text}"
            )
        else:
            print(f"POST /auth/claim/{other_player_id} (cross-club, persisted user) -> 404 (correct, rejected)")

        resp = client.post(f"/auth/claim/{manchester_temp_player_id}")
        if resp.status_code != 200:
            problems.append(
                f"POST /auth/claim/{manchester_temp_player_id} (same-club) expected 200, got {resp.status_code} {resp.text}"
            )
        else:
            print(f"POST /auth/claim/{manchester_temp_player_id} (same-club) -> 200 (correct, claimed)")
        with Session(engine) as db:
            refreshed = db.get(User, persisted_user_a.id)
            if refreshed.player_id != manchester_temp_player_id:
                problems.append(
                    f"After same-club claim, user.player_id={refreshed.player_id}, expected {manchester_temp_player_id}"
                )
            else:
                print(f"Confirmed persisted_user_a.player_id == {manchester_temp_player_id} in DB")

        # =====================================================================
        # Test group B: temp_user_b — for /auth/create-profile, must have no
        # linked player (create_profile requires user.player_id is None).
        # =====================================================================
        with Session(engine) as db:
            persisted_user_b = User(
                discord_id="test-verify-scoped-chunk2-999994",
                discord_name="Verify Script User B",
                player_id=None,
                is_super_admin=False,
                club_id=manchester_id,
            )
            db.add(persisted_user_b)
            db.commit()
            db.refresh(persisted_user_b)
            created_user_ids.append(persisted_user_b.id)

        app.dependency_overrides[auth.current_user] = lambda: persisted_user_b
        app.dependency_overrides[auth.require_user] = lambda: persisted_user_b

        resp = client.post("/auth/create-profile", json={"name": "ZZTest Chunk2 Created Player", "default_faction": None})
        if resp.status_code != 200:
            problems.append(f"POST /auth/create-profile failed: {resp.status_code} {resp.text}")
        else:
            new_player_id = resp.json()["player_id"]
            created_player_ids.append(new_player_id)
            with Session(engine) as db:
                new_player = db.get(Player, new_player_id)
                if new_player is None:
                    problems.append(f"New player id={new_player_id} not found after create-profile")
                elif new_player.club_id != manchester_id:
                    problems.append(
                        f"New player club_id={new_player.club_id}, expected {manchester_id} (from user.club_id, not placeholder)"
                    )
                else:
                    print(f"POST /auth/create-profile created player id={new_player_id} with club_id={new_player.club_id} (matches user.club_id)")

        # =====================================================================
        # Test group C: GET /auth/me with a real, existing Manchester user who
        # already has a linked player — read-only sanity check, no mutation.
        # =====================================================================
        with Session(engine) as db:
            real_linked_user = db.exec(
                select(User).where(User.club_id == manchester_id).where(User.player_id.is_not(None))
            ).first()

        if real_linked_user is None:
            problems.append("Expected at least one real Manchester user with a linked player, found none")
        else:
            app.dependency_overrides[auth.current_user] = lambda: real_linked_user
            resp = client.get("/auth/me")
            if resp.status_code != 200:
                problems.append(f"GET /auth/me (real linked user) failed: {resp.status_code} {resp.text}")
            else:
                body = resp.json()
                if body["claim_candidates"] != []:
                    problems.append(
                        f"GET /auth/me for a user with a linked player should return empty claim_candidates, got {body['claim_candidates']}"
                    )
                if body["player"] is None or body["player"]["id"] != real_linked_user.player_id:
                    problems.append("GET /auth/me did not return the correct linked player for a real user")
                else:
                    print(f"GET /auth/me (real linked user id={real_linked_user.id}) -> correct linked player, empty claim_candidates")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for uid in created_user_ids:
                u = db.get(User, uid)
                if u:
                    db.delete(u)
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
            final_users = db.exec(select(User)).all()
            final_players = db.exec(select(Player)).all()
            print(
                f"Cleanup done: clubs={len(final_clubs)} (expect 1, Manchester only), "
                f"users={len(final_users)} (expect 2, the original real users), "
                f"players={len(final_players)} (expect 2, the original real players)"
            )

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print(
            "\nVerification passed: GET /auth/me's claim_candidates is club-scoped, "
            "POST /auth/claim rejects cross-club players (404) and accepts same-club ones, "
            "and POST /auth/create-profile writes club_id from user.club_id."
        )


if __name__ == "__main__":
    main()
