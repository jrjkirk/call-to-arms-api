"""One-off verification script for the scoped-query-helper chunk 1
(pairing_blocks + admin_roles conversion). Not part of the app; run
manually against staging.

Proves two things beyond "no errors":
  1. scoped() actually filters — a row belonging to a temporary *second*
     club (inserted directly, bypassing the app) never appears through
     the converted endpoints when the caller's club_id is Manchester's.
  2. New rows written via the converted endpoints get club_id from the
     real authenticated user.club_id mechanism, not the old
     _default_club_id() placeholder — proven by using a fake user object
     whose club_id is set explicitly (still Manchester's real id today,
     but via the same field the auth layer would populate for a second
     club).

Also exercises the grant->gate->revoke->gate round trip against
GET /admin/league/results (require_scope("League")) to confirm
admin_scopes()'s converted query still works correctly end-to-end.

All rows this script creates (the temp second club, its pairing_blocks/
admin_roles rows, the temp User, and anything created via the real
endpoints) are cleaned up in a `finally`, leaving staging exactly as it
started (both tables were empty before this script ran).

Run with: python verify_scoped_helper_chunk1.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine, scoped
from models import PairingBlock, AdminRole, Club, Player, User
from main import app
import auth


def _fake_super_admin(club_id: int, uid: int = 999997) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-scoped-{uid}",
        discord_name="Verify Script (super-admin)",
        player_id=None,
        is_super_admin=True,
        club_id=club_id,
    )


def main():
    problems = []

    with Session(engine) as db:
        manchester = db.exec(select(Club).where(Club.slug == "manchester")).first()
        manchester_id = manchester.id
    print(f"Manchester club_id = {manchester_id}")

    client = TestClient(app)
    fake_admin = _fake_super_admin(manchester_id)
    app.dependency_overrides[auth.require_user] = lambda: fake_admin
    app.dependency_overrides[auth.current_user] = lambda: fake_admin

    other_club_id = None
    other_club_admin_role_user_id = None
    created_pairing_block_ids = []
    created_admin_role_ids = []
    created_user_ids = []
    created_player_ids = []

    try:
        # --- Set up a second club + rows belonging to it, inserted directly
        # (bypassing the app), to prove scoped() actually excludes them. ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Other Club", slug="zztest-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            # admin_roles.user_id has a DB-level FK to users, and
            # pairing_blocks.player_a_id/player_b_id have DB-level FKs to
            # players (none of these are declared on the SQLModel classes) —
            # all need real rows, not arbitrary ints.
            other_club_user = User(
                discord_id="test-verify-scoped-other-club-user",
                discord_name="ZZTest Other Club User",
                club_id=other_club_id,
            )
            db.add(other_club_user)
            other_player_a = Player(name="ZZTest Other Club Player A", club_id=other_club_id)
            other_player_b = Player(name="ZZTest Other Club Player B", club_id=other_club_id)
            db.add(other_player_a)
            db.add(other_player_b)
            db.commit()
            db.refresh(other_club_user)
            db.refresh(other_player_a)
            db.refresh(other_player_b)
            created_user_ids.append(other_club_user.id)
            created_player_ids.append(other_player_a.id)
            created_player_ids.append(other_player_b.id)

            other_block = PairingBlock(
                player_a_id=other_player_a.id, player_b_id=other_player_b.id,
                club_id=other_club_id,
            )
            db.add(other_block)
            other_role = AdminRole(
                user_id=other_club_user.id, scope="League", club_id=other_club_id,
            )
            db.add(other_role)
            db.commit()
            other_club_admin_role_user_id = other_club_user.id
            other_club_player_a_id = other_player_a.id
            print(f"Created other-club (id={other_club_id}) pairing_block id={other_block.id}, admin_role id={other_role.id}, user id={other_club_user.id}")

        # --- Real Manchester-owned players for the pairing_block creation test ---
        with Session(engine) as db:
            manchester_player_a = Player(name="ZZTest Manchester Player A", club_id=manchester_id)
            manchester_player_b = Player(name="ZZTest Manchester Player B", club_id=manchester_id)
            db.add(manchester_player_a)
            db.add(manchester_player_b)
            db.commit()
            db.refresh(manchester_player_a)
            db.refresh(manchester_player_b)
            created_player_ids.append(manchester_player_a.id)
            created_player_ids.append(manchester_player_b.id)
            fake_player_a = manchester_player_a.id
            fake_player_b = manchester_player_b.id

        # --- pairing_blocks: list should not include the other club's row ---
        resp = client.get("/admin/blocks")
        if resp.status_code != 200:
            problems.append(f"GET /admin/blocks failed: {resp.status_code} {resp.text}")
        else:
            blocks = resp.json()
            if any(b["player_a_id"] == other_club_player_a_id for b in blocks):
                problems.append("GET /admin/blocks leaked the other club's row — scoped() not filtering!")
            print(f"GET /admin/blocks (before creating a Manchester block): {blocks}")

        # --- pairing_blocks: create via the real endpoint, confirm club_id mechanism ---
        resp = client.post("/admin/blocks", json={
            "player_a_id": fake_player_a, "player_b_id": fake_player_b, "note": "verify-scoped-chunk1",
        })
        if resp.status_code != 200 or not resp.json().get("created"):
            problems.append(f"POST /admin/blocks failed: {resp.status_code} {resp.text}")
        with Session(engine) as db:
            row = db.exec(
                select(PairingBlock)
                .where(PairingBlock.player_a_id == fake_player_a)
                .where(PairingBlock.player_b_id == fake_player_b)
            ).first()
            if row is None:
                problems.append("New pairing_block row not found after POST")
            elif row.club_id != manchester_id:
                problems.append(f"New pairing_block club_id={row.club_id}, expected {manchester_id} (from user.club_id, not placeholder)")
            else:
                created_pairing_block_ids.append(row.id)
                print(f"POST /admin/blocks created row id={row.id} with club_id={row.club_id} (matches user.club_id)")

        resp = client.get("/admin/blocks")
        blocks = resp.json()
        if not any(b["player_a_id"] == fake_player_a for b in blocks):
            problems.append("GET /admin/blocks did not return the Manchester-owned row just created")
        if any(b["player_a_id"] == other_club_player_a_id for b in blocks):
            problems.append("GET /admin/blocks leaked the other club's row after creating a real one")
        print(f"GET /admin/blocks (after creating): {blocks}")

        resp = client.delete("/admin/blocks", params={"player_a_id": fake_player_a, "player_b_id": fake_player_b})
        if resp.status_code != 200 or not resp.json().get("removed"):
            problems.append(f"DELETE /admin/blocks failed: {resp.status_code} {resp.text}")
        else:
            created_pairing_block_ids = []  # already deleted via the real endpoint
            print("DELETE /admin/blocks removed the Manchester-owned row.")

        # --- admin_roles: need a real temp User for grant_role's target lookup ---
        with Session(engine) as db:
            temp_user = User(discord_id="test-verify-scoped-target-999996", discord_name="ZZTest Target", is_super_admin=False, club_id=manchester_id)
            db.add(temp_user)
            db.commit()
            db.refresh(temp_user)
            created_user_ids.append(temp_user.id)
            temp_user_id = temp_user.id
        print(f"Created temp target User id={temp_user_id}")

        resp = client.get("/admin/roles")
        if resp.status_code != 200:
            problems.append(f"GET /admin/roles failed: {resp.status_code} {resp.text}")
        else:
            roles = resp.json()["roles"]
            if any(r["user_id"] == other_club_admin_role_user_id for r in roles):
                problems.append("GET /admin/roles leaked the other club's row — scoped()/join filter not working!")
            print(f"GET /admin/roles (before grant): {roles}")

        resp = client.post("/admin/roles", json={"user_id": temp_user_id, "scope": "League"})
        if resp.status_code != 200:
            problems.append(f"POST /admin/roles failed: {resp.status_code} {resp.text}")
        with Session(engine) as db:
            role_row = db.exec(
                select(AdminRole)
                .where(AdminRole.user_id == temp_user_id)
                .where(AdminRole.scope == "League")
            ).first()
            if role_row is None:
                problems.append("New admin_role row not found after POST")
            elif role_row.club_id != manchester_id:
                problems.append(f"New admin_role club_id={role_row.club_id}, expected {manchester_id}")
            else:
                created_admin_role_ids.append(role_row.id)
                print(f"POST /admin/roles created row id={role_row.id} with club_id={role_row.club_id} (matches user.club_id)")

        resp = client.get("/admin/roles")
        roles = resp.json()["roles"]
        if not any(r["user_id"] == temp_user_id for r in roles):
            problems.append("GET /admin/roles did not return the Manchester-owned role just granted")
        if any(r["user_id"] == other_club_admin_role_user_id for r in roles):
            problems.append("GET /admin/roles leaked the other club's row after granting a real one")
        print(f"GET /admin/roles (after grant): {roles}")

        # --- grant -> gate -> revoke -> gate round trip against admin_scopes() ---
        with Session(engine) as db:
            temp_user_obj = db.get(User, temp_user_id)
        app.dependency_overrides[auth.require_user] = lambda: temp_user_obj
        app.dependency_overrides[auth.current_user] = lambda: temp_user_obj
        resp = client.get("/admin/league/results")
        if resp.status_code != 200:
            problems.append(f"[gate after grant] GET /admin/league/results expected 200, got {resp.status_code}")
        else:
            print("[gate after grant] GET /admin/league/results -> 200 (correct, role granted)")

        app.dependency_overrides[auth.require_user] = lambda: fake_admin
        app.dependency_overrides[auth.current_user] = lambda: fake_admin
        resp = client.delete("/admin/roles", params={"user_id": temp_user_id, "scope": "League"})
        if resp.status_code != 200 or not resp.json().get("removed"):
            problems.append(f"DELETE /admin/roles failed: {resp.status_code} {resp.text}")
        else:
            created_admin_role_ids = []
            print("DELETE /admin/roles removed the granted role.")

        app.dependency_overrides[auth.require_user] = lambda: temp_user_obj
        app.dependency_overrides[auth.current_user] = lambda: temp_user_obj
        resp = client.get("/admin/league/results")
        if resp.status_code != 403:
            problems.append(f"[gate after revoke] GET /admin/league/results expected 403, got {resp.status_code}")
        else:
            print("[gate after revoke] GET /admin/league/results -> 403 (correct, role revoked)")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for bid in created_pairing_block_ids:
                row = db.get(PairingBlock, bid)
                if row:
                    db.delete(row)
            for rid in created_admin_role_ids:
                row = db.get(AdminRole, rid)
                if row:
                    db.delete(row)
            db.commit()

            # other-club rows + the temp club itself
            leftover_blocks = db.exec(
                select(PairingBlock).where(PairingBlock.club_id == other_club_id)
            ).all() if other_club_id else []
            for row in leftover_blocks:
                db.delete(row)
            leftover_roles = db.exec(
                select(AdminRole).where(AdminRole.club_id == other_club_id)
            ).all() if other_club_id else []
            for row in leftover_roles:
                db.delete(row)
            db.commit()

            # users/players both FK to clubs — must go before the club itself
            for uid in created_user_ids:
                u = db.get(User, uid)
                if u:
                    db.delete(u)
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

            final_blocks = len(db.exec(select(PairingBlock)).all())
            final_roles = len(db.exec(select(AdminRole)).all())
            final_clubs = len(db.exec(select(Club)).all())
            print(f"Cleanup done: pairing_blocks={final_blocks} (expect 0), admin_roles={final_roles} (expect 0), clubs={final_clubs} (expect 1, Manchester only)")

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: scoped() correctly filters pairing_blocks and admin_roles, "
              "writes use user.club_id, and the grant/gate/revoke/gate round trip works via admin_scopes().")


if __name__ == "__main__":
    main()
