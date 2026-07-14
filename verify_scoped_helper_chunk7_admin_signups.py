"""One-off verification script for the scoped-query-helper phase, chunk 7
(admin.py's remaining signups endpoints). Not part of the app; run manually
against staging.

Proves, beyond "no errors":

  1. admin_signups_list: a Manchester caller's GET /admin/signups only ever
     returns Manchester's rows, with a second club's identically-shaped
     signup present for the same week/system the whole time.
  2. The 3 missing-ownership-check fixes, each proven via a direct row read
     (not just the status code):
       a. admin_signup_patch: PATCH on another club's signup id -> 404,
          row unmodified. Same-club patch still works (positive control).
       b. admin_signup_create: POST with a player_id belonging to another
          club -> 404, no row created. Same-club create still works and
          gets club_id=user.club_id.
       c. admin_signup_delete: DELETE on another club's signup id -> 404,
          row (and any prearranged Pairing referencing it) unmodified.
          Same-club delete still works, including cascading the prearranged
          Pairing delete.
  3. admin_signup_create's duplicate-guard (409) still works for a genuine
     same-club resubmission, and does NOT false-positive against another
     club's identically-shaped (same week/system/player_id-value) signup --
     proven by using a real second club's player with a numerically
     different player_id but colliding week/system.

All rows this script creates (a genuine second temp club + its players/
signups/pairings, temp Manchester players/signups) are cleaned up in a
`finally`, leaving staging exactly as it started.

Run with: python verify_scoped_helper_chunk7_admin_signups.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine, scoped
from models import Club, Player, Signup, Pairing, User
from main import app
import auth

TEST_SYSTEM = "The Old World"

WEEK_LIST = "26/01/2099"       # admin_signups_list isolation
WEEK_PATCH = "27/01/2099"      # admin_signup_patch ownership check
WEEK_CREATE = "28/01/2099"     # admin_signup_create ownership + duplicate-guard
WEEK_DELETE = "29/01/2099"     # admin_signup_delete ownership + cascade


def _fake_user(club_id: int, uid: int) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk7-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=None,
        is_super_admin=True,
        club_id=club_id,
    )


def main():
    problems = []
    client = TestClient(app)

    with Session(engine) as db:
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
    print(f"Manchester club_id = {manchester_id}")

    other_club_id = None
    created_player_ids = []
    created_signup_ids = []
    created_pairing_ids = []

    def _mk_player(name, club_id):
        with Session(engine) as db:
            p = Player(name=name, active=True, club_id=club_id)
            db.add(p)
            db.commit()
            db.refresh(p)
            created_player_ids.append(p.id)
            return p.id

    def _mk_signup(club_id, week, player_id, player_name, **kw):
        with Session(engine) as db:
            defaults = dict(standby_ok=False, tnt_ok=False, can_demo=False)
            defaults.update(kw)
            su = Signup(
                week=week, system=TEST_SYSTEM, player_id=player_id,
                player_name=player_name, club_id=club_id, **defaults,
            )
            db.add(su)
            db.commit()
            db.refresh(su)
            created_signup_ids.append(su.id)
            return su

    def _mk_pairing(club_id, week, a_id, b_id=None, **kw):
        with Session(engine) as db:
            defaults = dict(status="pending", prearranged=True)
            defaults.update(kw)
            p = Pairing(
                week=week, system=TEST_SYSTEM, a_signup_id=a_id, b_signup_id=b_id,
                club_id=club_id, **defaults,
            )
            db.add(p)
            db.commit()
            db.refresh(p)
            created_pairing_ids.append(p.id)
            return p

    def _get_signup(sid):
        with Session(engine) as db:
            return db.get(Signup, sid)

    def _get_pairing(pid):
        with Session(engine) as db:
            return db.get(Pairing, pid)

    try:
        # =====================================================================
        # 0. Second temp club
        # =====================================================================
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk7 Other Club", slug="zztest-chunk7-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id
        print(f"Created other club id={other_club_id}")

        manchester_admin = _fake_user(manchester_id, uid=999301)
        other_admin = _fake_user(other_club_id, uid=999302)

        def _as(user):
            app.dependency_overrides[auth.require_user] = lambda: user
            app.dependency_overrides[auth.current_user] = lambda: user

        # =====================================================================
        # 1. admin_signups_list isolation.
        # =====================================================================
        m_a = _mk_player("ZZTest Chunk7 M-List-A", manchester_id)
        o_a = _mk_player("ZZTest Chunk7 O-List-A", other_club_id)
        m_su_list = _mk_signup(manchester_id, WEEK_LIST, m_a, "ZZTest Chunk7 M-List-A")
        o_su_list = _mk_signup(other_club_id, WEEK_LIST, o_a, "ZZTest Chunk7 O-List-A")

        _as(manchester_admin)
        r = client.get("/admin/signups", params={"system": TEST_SYSTEM, "week": WEEK_LIST})
        if r.status_code != 200:
            problems.append(f"GET /admin/signups (manchester) failed: {r.status_code} {r.text}")
        else:
            ids_seen = {row["id"] for row in r.json()["signups"]}
            if o_su_list.id in ids_seen:
                problems.append(f"admin_signups_list LEAK: manchester's list included the other club's signup id {o_su_list.id}")
            if m_su_list.id not in ids_seen:
                problems.append(f"admin_signups_list: manchester's own signup id {m_su_list.id} missing from its own list")
            if o_su_list.id not in ids_seen and m_su_list.id in ids_seen:
                print("admin_signups_list: manchester sees only its own signup, other club's excluded -- OK")

        # =====================================================================
        # 2a. admin_signup_patch ownership check.
        # =====================================================================
        m_b = _mk_player("ZZTest Chunk7 M-Patch-B", manchester_id)
        o_b = _mk_player("ZZTest Chunk7 O-Patch-B", other_club_id)
        m_su_patch = _mk_signup(manchester_id, WEEK_PATCH, m_b, "ZZTest Chunk7 M-Patch-B", faction="Empire")
        o_su_patch = _mk_signup(other_club_id, WEEK_PATCH, o_b, "ZZTest Chunk7 O-Patch-B", faction="Chaos")

        _as(manchester_admin)
        r = client.patch(f"/admin/signups/{o_su_patch.id}", json={"faction": "HACKED"})
        if r.status_code != 404:
            problems.append(f"admin_signup_patch OWNERSHIP BUG: manchester caller patched other club's signup, got status {r.status_code} (expected 404)")
        else:
            print("admin_signup_patch: PATCH on other club's signup id correctly 404s -- OK")

        o_su_patch_after = _get_signup(o_su_patch.id)
        if o_su_patch_after.faction == "HACKED":
            problems.append("admin_signup_patch OWNERSHIP BUG: other club's signup row was actually modified despite the 404")
        else:
            print("admin_signup_patch: other club's signup row NOT modified (direct row read) -- OK")

        # Positive control: manchester patching its own signup works.
        r = client.patch(f"/admin/signups/{m_su_patch.id}", json={"faction": "High Elves"})
        if r.status_code != 200:
            problems.append(f"admin_signup_patch: own-club patch failed (positive control): {r.status_code} {r.text}")
        else:
            m_su_patch_after = _get_signup(m_su_patch.id)
            if m_su_patch_after.faction != "High Elves":
                problems.append(f"admin_signup_patch: own-club row was NOT updated as expected (positive control failed): {m_su_patch_after.faction}")
            else:
                print("admin_signup_patch: own-club row correctly updated (positive control) -- OK")

        # =====================================================================
        # 2b. admin_signup_create ownership check + duplicate-guard scoping.
        # =====================================================================
        m_c = _mk_player("ZZTest Chunk7 M-Create-C", manchester_id)
        o_c = _mk_player("ZZTest Chunk7 O-Create-C", other_club_id)

        _as(manchester_admin)
        r = client.post("/admin/signups", json={
            "system": TEST_SYSTEM, "week": WEEK_CREATE, "player_id": o_c,
        })
        if r.status_code != 404:
            problems.append(f"admin_signup_create OWNERSHIP BUG: manchester caller created a signup for another club's player, got status {r.status_code} (expected 404)")
        else:
            print("admin_signup_create: POST with another club's player_id correctly 404s -- OK")

        with Session(engine) as db:
            leaked = db.exec(
                select(Signup).where(Signup.week == WEEK_CREATE).where(Signup.system == TEST_SYSTEM).where(Signup.player_id == o_c)
            ).first()
        if leaked is not None:
            problems.append(f"admin_signup_create OWNERSHIP BUG: a signup row was actually created for the other club's player: {leaked}")
            created_signup_ids.append(leaked.id)
        else:
            print("admin_signup_create: no signup row created for the other club's player (direct row read) -- OK")

        # Other club creates its OWN signup for the same week/system (to
        # prove the duplicate-guard doesn't false-positive across clubs).
        _as(other_admin)
        r = client.post("/admin/signups", json={
            "system": TEST_SYSTEM, "week": WEEK_CREATE, "player_id": o_c,
        })
        if r.status_code != 201:
            problems.append(f"admin_signup_create: other club's own create failed: {r.status_code} {r.text}")
        else:
            o_created_id = r.json()["id"]
            created_signup_ids.append(o_created_id)
            with Session(engine) as db:
                o_row = db.get(Signup, o_created_id)
            if o_row.club_id != other_club_id:
                problems.append(f"admin_signup_create: other club's created row has wrong club_id: {o_row.club_id}")
            else:
                print(f"admin_signup_create: other club's own create succeeded with club_id={other_club_id} -- OK")

        # Manchester creates its own signup for m_c, same week/system.
        _as(manchester_admin)
        r = client.post("/admin/signups", json={
            "system": TEST_SYSTEM, "week": WEEK_CREATE, "player_id": m_c,
        })
        if r.status_code != 201:
            problems.append(f"admin_signup_create: manchester's own create failed (should not false-positive against other club's identically-shaped signup): {r.status_code} {r.text}")
        else:
            m_created_id = r.json()["id"]
            created_signup_ids.append(m_created_id)
            with Session(engine) as db:
                m_row = db.get(Signup, m_created_id)
            if m_row.club_id != manchester_id:
                problems.append(f"admin_signup_create: manchester's created row has wrong club_id: {m_row.club_id}")
            else:
                print(f"admin_signup_create: manchester's own create succeeded with club_id={manchester_id}, unaffected by other club's identically-shaped signup -- OK")

        # Duplicate-guard: manchester resubmitting for m_c/same week/system -> 409.
        r = client.post("/admin/signups", json={
            "system": TEST_SYSTEM, "week": WEEK_CREATE, "player_id": m_c,
        })
        if r.status_code != 409:
            problems.append(f"admin_signup_create: duplicate-guard should 409 on a genuine same-club resubmit, got {r.status_code} {r.text}")
        else:
            print("admin_signup_create: duplicate-guard correctly 409s on a genuine same-club resubmit -- OK")

        # =====================================================================
        # 2c. admin_signup_delete ownership check, including prearranged
        #     Pairing cascade scoping.
        # =====================================================================
        m_d1 = _mk_player("ZZTest Chunk7 M-Delete-D1", manchester_id)
        m_d2 = _mk_player("ZZTest Chunk7 M-Delete-D2", manchester_id)
        o_d1 = _mk_player("ZZTest Chunk7 O-Delete-D1", other_club_id)
        o_d2 = _mk_player("ZZTest Chunk7 O-Delete-D2", other_club_id)

        m_su_d1 = _mk_signup(manchester_id, WEEK_DELETE, m_d1, "ZZTest Chunk7 M-Delete-D1")
        m_su_d2 = _mk_signup(manchester_id, WEEK_DELETE, m_d2, "ZZTest Chunk7 M-Delete-D2")
        o_su_d1 = _mk_signup(other_club_id, WEEK_DELETE, o_d1, "ZZTest Chunk7 O-Delete-D1")
        o_su_d2 = _mk_signup(other_club_id, WEEK_DELETE, o_d2, "ZZTest Chunk7 O-Delete-D2")

        m_prearranged = _mk_pairing(manchester_id, WEEK_DELETE, m_su_d1.id, m_su_d2.id)
        o_prearranged = _mk_pairing(other_club_id, WEEK_DELETE, o_su_d1.id, o_su_d2.id)

        _as(manchester_admin)
        r = client.delete(f"/admin/signups/{o_su_d1.id}")
        if r.status_code != 404:
            problems.append(f"admin_signup_delete OWNERSHIP BUG: manchester caller deleted other club's signup, got status {r.status_code} (expected 404)")
        else:
            print("admin_signup_delete: DELETE on other club's signup id correctly 404s -- OK")

        o_su_d1_after = _get_signup(o_su_d1.id)
        if o_su_d1_after is None:
            problems.append("admin_signup_delete OWNERSHIP BUG: other club's signup row was actually deleted despite the 404")
        else:
            print("admin_signup_delete: other club's signup row NOT deleted (direct row read) -- OK")

        o_prearranged_after = _get_pairing(o_prearranged.id)
        if o_prearranged_after is None:
            problems.append("admin_signup_delete OWNERSHIP BUG: other club's prearranged Pairing was deleted as a cascade side-effect of the rejected cross-club delete")
        else:
            print("admin_signup_delete: other club's prearranged Pairing NOT deleted (direct row read) -- OK")

        # Positive control: manchester deleting its own signup works, and
        # correctly cascades the prearranged Pairing delete.
        r = client.delete(f"/admin/signups/{m_su_d1.id}")
        if r.status_code != 200:
            problems.append(f"admin_signup_delete: own-club delete failed (positive control): {r.status_code} {r.text}")
        else:
            m_su_d1_after = _get_signup(m_su_d1.id)
            m_prearranged_after = _get_pairing(m_prearranged.id)
            if m_su_d1_after is not None:
                problems.append("admin_signup_delete: own-club signup row still present after a reported successful delete")
            elif m_prearranged_after is not None:
                problems.append("admin_signup_delete: own-club prearranged Pairing was NOT cascaded-deleted as expected")
            else:
                print("admin_signup_delete: own-club signup deleted and its prearranged Pairing correctly cascaded -- OK")
                created_signup_ids.remove(m_su_d1.id)
                created_pairing_ids.remove(m_prearranged.id)

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
            for plid in created_player_ids:
                pl = db.get(Player, plid)
                if pl:
                    db.delete(pl)
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
            "\nVerification passed: admin_signups_list is correctly isolated per "
            "club, and all 3 ownership-check fixes (patch/create/delete) correctly "
            "404 for cross-club access with direct row reads confirming nothing was "
            "modified/created/deleted, while same-club patch/create/delete all still "
            "work, and the duplicate-guard on create still works correctly without "
            "false-positiving against another club's identically-shaped signup."
        )


if __name__ == "__main__":
    main()
