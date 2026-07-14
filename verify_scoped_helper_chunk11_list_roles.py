"""One-off verification script for the scoped-query-helper chunk 11
(admin.py::list_roles' super_admins_rows fix). Not part of the app; run
manually against staging.

Closes the one known gap flagged at the end of chunk 10: list_roles()'s
second query (super_admins_rows) was never scoped by club_id, so a
Manchester caller would see every club's super-admins, not just their own.
The main AdminRole+User join in the same endpoint was already scoped in
chunk 1 and is untouched by this fix — re-verified here anyway since it's
the same endpoint.

Proves:
  1. A genuine second temp club with its own super-admin user is NOT
     included in GET /admin/roles' "super_admins" list when called as a
     Manchester caller.
  2. Manchester's own super-admin(s) (including the fake caller used to
     hit the endpoint) ARE still correctly listed.
  3. The main AdminRole+User join part of the response is unaffected.

All rows created (temp club, temp users) are cleaned up in a `finally`.

Run with: python verify_scoped_helper_chunk11_list_roles.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, User
from main import app
import auth


def main():
    problems = []

    with Session(engine) as db:
        manchester = db.exec(select(Club).where(Club.slug == "manchester")).first()
        manchester_id = manchester.id
    print(f"Manchester club_id = {manchester_id}")

    client = TestClient(app)

    other_club_id = None
    created_user_ids = []

    try:
        # --- a real, persisted Manchester super-admin row, used as the caller.
        # Needs to actually exist in the DB (not just an in-memory fake) so
        # scoped(User, ...) has a real matching row to find and prove
        # positive inclusion, not just absence of the other club's row. ---
        with Session(engine) as db:
            manchester_super_admin = User(
                discord_id="test-verify-scoped-ch11-manchester-sa",
                discord_name="ZZTest Manchester Super Admin",
                is_super_admin=True,
                club_id=manchester_id,
            )
            db.add(manchester_super_admin)
            db.commit()
            db.refresh(manchester_super_admin)
            created_user_ids.append(manchester_super_admin.id)
            fake_admin = manchester_super_admin
            print(f"Created Manchester (id={manchester_id}) super-admin caller user id={fake_admin.id}")

        app.dependency_overrides[auth.require_super_admin] = lambda: fake_admin
        app.dependency_overrides[auth.require_user] = lambda: fake_admin
        app.dependency_overrides[auth.current_user] = lambda: fake_admin

        # --- second club + its own super-admin user, inserted directly ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Other Club Ch11", slug="zztest-other-club-ch11")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            other_super_admin = User(
                discord_id="test-verify-scoped-ch11-other-club-sa",
                discord_name="ZZTest Other Club Super Admin",
                is_super_admin=True,
                club_id=other_club_id,
            )
            db.add(other_super_admin)
            db.commit()
            db.refresh(other_super_admin)
            created_user_ids.append(other_super_admin.id)
            other_super_admin_id = other_super_admin.id
            print(f"Created other-club (id={other_club_id}) super-admin user id={other_super_admin_id}")

        resp = client.get("/admin/roles")
        if resp.status_code != 200:
            problems.append(f"GET /admin/roles failed: {resp.status_code} {resp.text}")
        else:
            body = resp.json()
            super_admin_ids = {sa["user_id"] for sa in body["super_admins"]}
            print(f"super_admins user_ids returned: {super_admin_ids}")

            if other_super_admin_id in super_admin_ids:
                problems.append(
                    "GET /admin/roles leaked the other club's super-admin — "
                    "super_admins_rows still unscoped!"
                )
            else:
                print("Other club's super-admin correctly excluded.")

            if fake_admin.id not in super_admin_ids:
                problems.append(
                    "GET /admin/roles did not include the caller's own (Manchester) "
                    "super-admin — over-filtering, not just under-filtering."
                )
            else:
                print("Manchester's own super-admin (caller) correctly included.")

            # main AdminRole+User join part of the response, unaffected by this fix
            if "roles" not in body:
                problems.append("GET /admin/roles response missing 'roles' key (main join broken)")
            else:
                print(f"roles (AdminRole+User join, unaffected by this fix): {body['roles']}")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for uid in created_user_ids:
                u = db.get(User, uid)
                if u:
                    db.delete(u)
            db.commit()

            if other_club_id:
                club = db.get(Club, other_club_id)
                if club:
                    db.delete(club)
                    db.commit()

            final_clubs = len(db.exec(select(Club)).all())
            final_users = len(db.exec(select(User)).all())
            print(f"Cleanup done: clubs={final_clubs} (expect 1, Manchester only), users={final_users} (expect 2, real users only)")

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: GET /admin/roles' super_admins list is correctly "
              "scoped to the caller's club — the last known gap in the scoped-query-helper "
              "phase's file sweep is closed.")


if __name__ == "__main__":
    main()
