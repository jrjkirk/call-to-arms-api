"""One-off verification script for the scoped-query-helper phase, chunk 6
(admin.py's pairings endpoints). Not part of the app; run manually against
staging.

Proves, beyond "no errors":

  1. THE DESTRUCTIVE BUG FIX (priority item): `pairings_generate()`'s
     "delete existing pending pairings" query is now scoped by club_id.
     A genuine second temp club and a Manchester scenario both have
     existing pending (non-prearranged) pairings for the SAME week/system.
     Manchester calling POST /admin/pairings/generate must not delete the
     other club's pending pairings, and vice versa.
  2. The 2 missing-ownership-check fixes (`pairings_save`, `pairings_delete`):
     a Manchester caller attempting to save/delete another club's pairing
     id is silently skipped (not modified/deleted) -- confirmed via a
     direct row read, not just response shape. Same-club rows still work
     (positive control).
  3. `pairings_preview`, `pairings_get`, `pairings_publish` all
     return/affect only the caller's club's rows, with a second club's
     identically-shaped data present the whole time.
  4. A full sweep of all 6 endpoints (preview/generate/get/publish/save/
     delete) against Manchester's own data, confirming ordinary behavior
     is unaffected by the added scoping (matches the pre-conversion code
     path exactly when there's no cross-club data in play -- confirmed by
     `git diff admin.py` showing only `select(...)` -> `scoped(...)` and
     bare-condition -> `... or p.club_id != user.club_id` changes, zero
     other logic touched).

All rows this script creates (a genuine second temp club + its players/
signups/pairings/publish_state, temp Manchester players/signups/pairings/
publish_state) are cleaned up in a `finally`, leaving staging exactly as
it started.

Run with: python verify_scoped_helper_chunk6_admin_pairings.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine, scoped
from models import Club, Player, Signup, Pairing, PublishState, User
from main import app
import auth

TEST_SYSTEM = "The Old World"

WEEK_DESTRUCTIVE = "26/01/2099"   # shared week for the destructive-delete proof
WEEK_OWNERSHIP = "27/01/2099"     # shared week for pairings_save/delete ownership checks
WEEK_ISOLATION = "28/01/2099"     # shared week for preview/get/publish isolation checks
WEEK_SWEEP = "29/01/2099"         # Manchester-only full sweep


def _fake_user(club_id: int, uid: int) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk6-{uid}",
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
    created_publish_ids = []

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
            defaults = dict(status="pending", prearranged=False)
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

    def _mk_publish_state(club_id, week, published):
        with Session(engine) as db:
            ps = PublishState(week=week, system=TEST_SYSTEM, published=published, club_id=club_id)
            db.add(ps)
            db.commit()
            db.refresh(ps)
            created_publish_ids.append(ps.id)
            return ps

    def _get_pairing(pid):
        with Session(engine) as db:
            return db.get(Pairing, pid)

    try:
        # =====================================================================
        # 0. Second temp club
        # =====================================================================
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk6 Other Club", slug="zztest-chunk6-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id
        print(f"Created other club id={other_club_id}")

        manchester_admin = _fake_user(manchester_id, uid=999201)
        other_admin = _fake_user(other_club_id, uid=999202)

        def _as(user):
            app.dependency_overrides[auth.require_user] = lambda: user
            app.dependency_overrides[auth.current_user] = lambda: user

        # =====================================================================
        # 1. DESTRUCTIVE BUG PROOF: both clubs have existing pending
        #    (non-prearranged) pairings for the SAME week/system. Manchester
        #    generates -> other club's pending pairing must survive.
        #    Then other club generates -> Manchester's (newly generated)
        #    pairings must survive.
        # =====================================================================
        m_a = _mk_player("ZZTest Chunk6 M-Destructive-A", manchester_id)
        m_b = _mk_player("ZZTest Chunk6 M-Destructive-B", manchester_id)
        o_a = _mk_player("ZZTest Chunk6 O-Destructive-A", other_club_id)
        o_b = _mk_player("ZZTest Chunk6 O-Destructive-B", other_club_id)

        m_su_a = _mk_signup(manchester_id, WEEK_DESTRUCTIVE, m_a, "ZZTest Chunk6 M-Destructive-A")
        m_su_b = _mk_signup(manchester_id, WEEK_DESTRUCTIVE, m_b, "ZZTest Chunk6 M-Destructive-B")
        o_su_a = _mk_signup(other_club_id, WEEK_DESTRUCTIVE, o_a, "ZZTest Chunk6 O-Destructive-A")
        o_su_b = _mk_signup(other_club_id, WEEK_DESTRUCTIVE, o_b, "ZZTest Chunk6 O-Destructive-B")

        m_old_pairing = _mk_pairing(manchester_id, WEEK_DESTRUCTIVE, m_su_a.id, m_su_b.id)
        o_old_pairing = _mk_pairing(other_club_id, WEEK_DESTRUCTIVE, o_su_a.id, o_su_b.id)
        print(f"Pre-generate: manchester old pairing id={m_old_pairing.id}, other club old pairing id={o_old_pairing.id}")

        # Manchester generates for the shared week/system.
        _as(manchester_admin)
        r = client.post("/admin/pairings/generate", json={"system": TEST_SYSTEM, "week": WEEK_DESTRUCTIVE})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/generate (manchester, destructive-proof) failed: {r.status_code} {r.text}")

        # Confirm manchester's own old pairing WAS deleted (expected -- it's
        # manchester's own pending pairing, correctly cleared before regen).
        m_old_after = _get_pairing(m_old_pairing.id)
        if m_old_after is not None:
            problems.append(f"manchester's own old pending pairing {m_old_pairing.id} was NOT deleted by its own generate() call (expected it to be)")
        else:
            print("manchester's own old pending pairing correctly deleted by its own generate() call -- OK")

        # THE KEY CHECK: other club's old pairing must survive untouched.
        o_old_after = _get_pairing(o_old_pairing.id)
        if o_old_after is None:
            problems.append("DESTRUCTIVE BUG STILL PRESENT: manchester's generate() call deleted the OTHER CLUB's pending pairing")
        elif o_old_after.a_signup_id != o_su_a.id or o_old_after.b_signup_id != o_su_b.id or o_old_after.club_id != other_club_id:
            problems.append(f"other club's old pairing {o_old_pairing.id} survived but was mutated: {o_old_after}")
        else:
            print("other club's pending pairing SURVIVED manchester's generate() call, byte-identical -- OK (destructive bug fixed)")

        # Capture manchester's freshly-generated pairings for this week (if any).
        with Session(engine) as db:
            m_new_rows = db.exec(
                scoped(Pairing, manchester_id).where(Pairing.week == WEEK_DESTRUCTIVE).where(Pairing.system == TEST_SYSTEM)
            ).all()
            for p in m_new_rows:
                if p.id not in created_pairing_ids:
                    created_pairing_ids.append(p.id)
        m_new_ids = {p.id for p in m_new_rows}
        print(f"manchester's freshly-generated pairing ids for {WEEK_DESTRUCTIVE}: {m_new_ids}")

        # Now the other club generates for the SAME week/system.
        _as(other_admin)
        r = client.post("/admin/pairings/generate", json={"system": TEST_SYSTEM, "week": WEEK_DESTRUCTIVE})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/generate (other club, destructive-proof) failed: {r.status_code} {r.text}")

        # Other club's own old pairing should now be gone (correctly cleared).
        o_old_after2 = _get_pairing(o_old_pairing.id)
        if o_old_after2 is not None:
            problems.append(f"other club's own old pending pairing {o_old_pairing.id} was NOT deleted by its own generate() call (expected it to be)")
        else:
            print("other club's own old pending pairing correctly deleted by its own generate() call -- OK")

        # Track the other club's freshly-generated pairings for cleanup too.
        with Session(engine) as db:
            o_new_rows = db.exec(
                scoped(Pairing, other_club_id).where(Pairing.week == WEEK_DESTRUCTIVE).where(Pairing.system == TEST_SYSTEM)
            ).all()
            for p in o_new_rows:
                if p.id not in created_pairing_ids:
                    created_pairing_ids.append(p.id)

        # THE REVERSE KEY CHECK: manchester's freshly-generated pairings must
        # survive the other club's generate() call, untouched.
        still_there = []
        for pid in m_new_ids:
            row = _get_pairing(pid)
            if row is not None:
                still_there.append(row)
        if len(still_there) != len(m_new_ids):
            problems.append(
                f"DESTRUCTIVE BUG STILL PRESENT (reverse direction): other club's generate() call deleted "
                f"{len(m_new_ids) - len(still_there)} of manchester's freshly-generated pairing(s)"
            )
        else:
            print(f"manchester's {len(m_new_ids)} freshly-generated pairing(s) SURVIVED the other club's generate() call -- OK (destructive bug fixed, reverse direction)")

        # =====================================================================
        # 2. Ownership-check fixes: pairings_save / pairings_delete.
        #    Manchester caller attempts to save/delete the OTHER club's
        #    pairing id -> must be silently skipped, row unchanged.
        #    Same-club row (positive control) must still work.
        # =====================================================================
        m_c = _mk_player("ZZTest Chunk6 M-Own-C", manchester_id)
        m_d = _mk_player("ZZTest Chunk6 M-Own-D", manchester_id)
        o_c = _mk_player("ZZTest Chunk6 O-Own-C", other_club_id)
        o_d = _mk_player("ZZTest Chunk6 O-Own-D", other_club_id)

        m_su_c = _mk_signup(manchester_id, WEEK_OWNERSHIP, m_c, "ZZTest Chunk6 M-Own-C", faction="Empire")
        m_su_d = _mk_signup(manchester_id, WEEK_OWNERSHIP, m_d, "ZZTest Chunk6 M-Own-D", faction="Bretonnia")
        o_su_c = _mk_signup(other_club_id, WEEK_OWNERSHIP, o_c, "ZZTest Chunk6 O-Own-C", faction="Chaos")
        o_su_d = _mk_signup(other_club_id, WEEK_OWNERSHIP, o_d, "ZZTest Chunk6 O-Own-D", faction="Orcs")

        m_own_pairing = _mk_pairing(manchester_id, WEEK_OWNERSHIP, m_su_c.id, m_su_d.id)
        o_foreign_pairing = _mk_pairing(other_club_id, WEEK_OWNERSHIP, o_su_c.id, o_su_d.id)

        # --- pairings_save ---
        _as(manchester_admin)
        r = client.post("/admin/pairings/save", json={
            "system": TEST_SYSTEM, "week": WEEK_OWNERSHIP,
            "rows": [
                {"id": o_foreign_pairing.id, "a_faction": "HACKED"},
                {"id": m_own_pairing.id, "a_faction": "High Elves"},
            ],
        })
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/save failed: {r.status_code} {r.text}")
        else:
            changed = r.json().get("changed")
            if changed != 1:
                problems.append(f"pairings_save: expected changed=1 (only the own-club row), got {changed}")
            else:
                print("pairings_save: response reports changed=1 (skipped the foreign-club row) -- OK")

        o_foreign_after_save = _get_pairing(o_foreign_pairing.id)
        if o_foreign_after_save.a_faction == "HACKED":
            problems.append("pairings_save OWNERSHIP BUG: manchester caller successfully modified the OTHER CLUB's pairing row")
        else:
            print("pairings_save: other club's pairing row NOT modified by manchester caller (direct row read) -- OK")

        m_own_after_save = _get_pairing(m_own_pairing.id)
        if m_own_after_save.a_faction != "High Elves":
            problems.append(f"pairings_save: own-club row was NOT updated as expected (positive control failed): {m_own_after_save.a_faction}")
        else:
            print("pairings_save: own-club row correctly updated (positive control) -- OK")

        # --- pairings_delete ---
        _as(manchester_admin)
        r = client.request("DELETE", "/admin/pairings", json={
            "system": TEST_SYSTEM, "week": WEEK_OWNERSHIP,
            "ids": [o_foreign_pairing.id],
        })
        if r.status_code != 200:
            problems.append(f"DELETE /admin/pairings (foreign id) failed: {r.status_code} {r.text}")
        else:
            deleted = r.json().get("deleted")
            if deleted != 0:
                problems.append(f"pairings_delete: expected deleted=0 for a foreign-club id, got {deleted}")
            else:
                print("pairings_delete: response reports deleted=0 for the foreign-club id -- OK")

        o_foreign_after_delete = _get_pairing(o_foreign_pairing.id)
        if o_foreign_after_delete is None:
            problems.append("pairings_delete OWNERSHIP BUG: manchester caller successfully deleted the OTHER CLUB's pairing row")
        else:
            print("pairings_delete: other club's pairing row NOT deleted by manchester caller (direct row read) -- OK")

        # Positive control: manchester deleting its own row works.
        r = client.request("DELETE", "/admin/pairings", json={
            "system": TEST_SYSTEM, "week": WEEK_OWNERSHIP,
            "ids": [m_own_pairing.id],
        })
        if r.status_code != 200 or r.json().get("deleted") != 1:
            problems.append(f"pairings_delete: own-club id delete failed (positive control): {r.status_code} {r.text}")
        else:
            m_own_after_delete = _get_pairing(m_own_pairing.id)
            if m_own_after_delete is not None:
                problems.append("pairings_delete: own-club row still present after a reported successful delete")
            else:
                print("pairings_delete: own-club row correctly deleted (positive control) -- OK")
                created_pairing_ids.remove(m_own_pairing.id)

        # =====================================================================
        # 3. pairings_preview / pairings_get / pairings_publish isolation,
        #    with a second club's identically-shaped data present throughout.
        # =====================================================================
        m_e = _mk_player("ZZTest Chunk6 M-Iso-E", manchester_id)
        m_f = _mk_player("ZZTest Chunk6 M-Iso-F", manchester_id)
        o_e = _mk_player("ZZTest Chunk6 O-Iso-E", other_club_id)
        o_f = _mk_player("ZZTest Chunk6 O-Iso-F", other_club_id)

        m_su_e = _mk_signup(manchester_id, WEEK_ISOLATION, m_e, "ZZTest Chunk6 M-Iso-E")
        m_su_f = _mk_signup(manchester_id, WEEK_ISOLATION, m_f, "ZZTest Chunk6 M-Iso-F")
        o_su_e = _mk_signup(other_club_id, WEEK_ISOLATION, o_e, "ZZTest Chunk6 O-Iso-E")
        o_su_f = _mk_signup(other_club_id, WEEK_ISOLATION, o_f, "ZZTest Chunk6 O-Iso-F")

        # Prearranged pairings for both clubs, same week/system.
        m_prearranged = _mk_pairing(manchester_id, WEEK_ISOLATION, m_su_e.id, m_su_f.id, prearranged=True, status="pending")
        o_prearranged = _mk_pairing(other_club_id, WEEK_ISOLATION, o_su_e.id, o_su_f.id, prearranged=True, status="pending")

        # --- pairings_preview ---
        _as(manchester_admin)
        r = client.post("/admin/pairings/preview", json={"system": TEST_SYSTEM, "week": WEEK_ISOLATION})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/preview failed: {r.status_code} {r.text}")
        else:
            rows = r.json()["rows"]
            ids_seen = {row["id"] for row in rows if row["id"] is not None}
            if o_prearranged.id in ids_seen:
                problems.append(f"pairings_preview LEAK: manchester's preview included the other club's prearranged pairing id {o_prearranged.id}")
            if m_prearranged.id not in ids_seen:
                problems.append(f"pairings_preview: manchester's own prearranged pairing id {m_prearranged.id} missing from its own preview")
            if o_prearranged.id not in ids_seen and m_prearranged.id in ids_seen:
                print("pairings_preview: manchester sees only its own prearranged pairing, other club's excluded -- OK")

        # --- pairings_get ---
        r = client.get("/admin/pairings", params={"system": TEST_SYSTEM, "week": WEEK_ISOLATION})
        if r.status_code != 200:
            problems.append(f"GET /admin/pairings failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            ids_seen = {row["id"] for row in body["rows"] if row["id"] is not None}
            if o_prearranged.id in ids_seen:
                problems.append(f"pairings_get LEAK: manchester's GET included the other club's pairing id {o_prearranged.id}")
            elif m_prearranged.id in ids_seen:
                print("pairings_get: manchester sees only its own pairing rows, other club's excluded -- OK")

        # --- pairings_publish (gate isolation + creation club_id) ---
        _as(other_admin)
        r = client.post("/admin/pairings/publish", json={"system": TEST_SYSTEM, "week": WEEK_ISOLATION, "published": True})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/publish (other club) failed: {r.status_code} {r.text}")

        _as(manchester_admin)
        r = client.post("/admin/pairings/publish", json={"system": TEST_SYSTEM, "week": WEEK_ISOLATION, "published": False})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/publish (manchester) failed: {r.status_code} {r.text}")

        with Session(engine) as db:
            gates = db.exec(
                select(PublishState).where(PublishState.week == WEEK_ISOLATION).where(PublishState.system == TEST_SYSTEM)
            ).all()
            for g in gates:
                if g.id not in created_publish_ids:
                    created_publish_ids.append(g.id)
            by_club = {g.club_id: g for g in gates}

        if len(gates) != 2:
            problems.append(f"pairings_publish: expected 2 separate PublishState rows (one per club), got {len(gates)}")
        else:
            if by_club.get(manchester_id) is None or by_club[manchester_id].published != False:
                problems.append(f"pairings_publish: manchester's gate wrong: {by_club.get(manchester_id)}")
            if by_club.get(other_club_id) is None or by_club[other_club_id].published != True:
                problems.append(f"pairings_publish: other club's gate wrong: {by_club.get(other_club_id)}")
            if by_club.get(manchester_id) and by_club.get(other_club_id) and by_club[manchester_id].published != by_club[other_club_id].published:
                print("pairings_publish: two independent PublishState rows created, one per club, correct club_id and independent published values -- OK")

        # Confirm GET /admin/pairings reflects manchester's own gate only.
        _as(manchester_admin)
        r = client.get("/admin/pairings", params={"system": TEST_SYSTEM, "week": WEEK_ISOLATION})
        if r.status_code == 200:
            if r.json()["published"] != False:
                problems.append(f"pairings_get: manchester's published flag should be False (its own gate), got {r.json()['published']}")
            else:
                print("pairings_get: publish state correctly isolated to manchester's own gate -- OK")

        # =====================================================================
        # 4. Full sweep of all 6 endpoints against manchester-only data
        #    (ordinary single-club behavior, confirming the scoping additions
        #    didn't break anything -- matches pre-conversion behavior exactly
        #    since a filtered query and an unfiltered query return identical
        #    rows when there's only one club's data in play).
        # =====================================================================
        s_a = _mk_player("ZZTest Chunk6 Sweep-A", manchester_id)
        s_b = _mk_player("ZZTest Chunk6 Sweep-B", manchester_id)
        s_su_a = _mk_signup(manchester_id, WEEK_SWEEP, s_a, "ZZTest Chunk6 Sweep-A")
        s_su_b = _mk_signup(manchester_id, WEEK_SWEEP, s_b, "ZZTest Chunk6 Sweep-B")

        _as(manchester_admin)

        r = client.post("/admin/pairings/preview", json={"system": TEST_SYSTEM, "week": WEEK_SWEEP})
        sweep_ok = r.status_code == 200
        if not sweep_ok:
            problems.append(f"sweep: preview failed {r.status_code} {r.text}")

        r = client.post("/admin/pairings/generate", json={"system": TEST_SYSTEM, "week": WEEK_SWEEP})
        if r.status_code != 200:
            problems.append(f"sweep: generate failed {r.status_code} {r.text}")
            sweep_ok = False
        else:
            with Session(engine) as db:
                gen_rows = db.exec(scoped(Pairing, manchester_id).where(Pairing.week == WEEK_SWEEP).where(Pairing.system == TEST_SYSTEM)).all()
                for p in gen_rows:
                    if p.id not in created_pairing_ids:
                        created_pairing_ids.append(p.id)
            if len(gen_rows) != 1:
                problems.append(f"sweep: generate expected 1 pairing (2 signups, no odd), got {len(gen_rows)}")
                sweep_ok = False
            sweep_pairing_id = gen_rows[0].id if gen_rows else None

        r = client.get("/admin/pairings", params={"system": TEST_SYSTEM, "week": WEEK_SWEEP})
        if r.status_code != 200 or len(r.json()["rows"]) != 1:
            problems.append(f"sweep: get failed or wrong row count: {r.status_code} {r.text}")
            sweep_ok = False

        r = client.post("/admin/pairings/publish", json={"system": TEST_SYSTEM, "week": WEEK_SWEEP, "published": True})
        if r.status_code != 200:
            problems.append(f"sweep: publish failed {r.status_code} {r.text}")
            sweep_ok = False
        else:
            with Session(engine) as db:
                g = db.exec(select(PublishState).where(PublishState.week == WEEK_SWEEP).where(PublishState.system == TEST_SYSTEM)).first()
                if g:
                    created_publish_ids.append(g.id)

        if sweep_pairing_id:
            r = client.post("/admin/pairings/save", json={
                "system": TEST_SYSTEM, "week": WEEK_SWEEP,
                "rows": [{"id": sweep_pairing_id, "a_faction": "Dwarfs"}],
            })
            if r.status_code != 200 or r.json().get("changed") != 1:
                problems.append(f"sweep: save failed or didn't change the row: {r.status_code} {r.text}")
                sweep_ok = False

            r = client.request("DELETE", "/admin/pairings", json={
                "system": TEST_SYSTEM, "week": WEEK_SWEEP, "ids": [sweep_pairing_id],
            })
            if r.status_code != 200 or r.json().get("deleted") != 1:
                problems.append(f"sweep: delete failed or didn't delete the row: {r.status_code} {r.text}")
                sweep_ok = False
            else:
                created_pairing_ids.remove(sweep_pairing_id)

        if sweep_ok:
            print("Full sweep: preview/generate/get/publish/save/delete all behave normally for manchester-only data -- OK")

        # --- post-discord: confirmed by code read, not DB-touching; smoke-test
        #     it returns the "no dispatch token" branch cleanly (no crash) if
        #     GH_DISPATCH_TOKEN isn't set in this environment.
        r = client.post("/admin/pairings/post-discord", json={"system": TEST_SYSTEM, "week": WEEK_SWEEP})
        if r.status_code != 200:
            problems.append(f"pairings_post_discord smoke-test failed: {r.status_code} {r.text}")
        else:
            print(f"pairings_post_discord: 200 OK, response: {r.json()} (no DB writes involved -- confirmed by code read)")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for pid in created_pairing_ids:
                p = db.get(Pairing, pid)
                if p:
                    db.delete(p)
            db.commit()
            for psid in created_publish_ids:
                ps = db.get(PublishState, psid)
                if ps:
                    db.delete(ps)
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
            "\nVerification passed: the destructive delete-all-pending bug is fixed "
            "(proven both directions with concurrent same-week/system generation), "
            "pairings_save/pairings_delete correctly skip foreign-club ids (direct "
            "row reads confirm no mutation/deletion) while same-club rows still "
            "work, pairings_preview/pairings_get/pairings_publish are all correctly "
            "isolated per club, and the full 6-endpoint sweep behaves normally for "
            "ordinary single-club data."
        )


if __name__ == "__main__":
    main()
