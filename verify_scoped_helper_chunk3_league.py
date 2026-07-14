"""One-off verification script for the scoped-query-helper chunk 3
(league.py + admin.py's league-result endpoints). Not part of the app;
run manually against staging.

Proves, beyond "no errors":
  1. POST /league/results rejects (404) submitting a result where either
     player belongs to a different club than the caller, even though both
     player ids are real and active — just not in the caller's club.
  2. POST /league/results still succeeds for two players in the caller's
     own club, and the created row's club_id is the caller's real
     user.club_id (not the old _default_club_id() placeholder).
  3. PATCH /admin/league/results/{id} and DELETE /admin/league/results/{id}
     reject (404) a result belonging to a different club, using a genuine
     second temporary club's own LeagueResult row (inserted directly,
     bypassing the app).
  4. PATCH /admin/league/results/{id} also rejects (404) patching in a
     player_1_id/player_2_id belonging to a different club, on an
     otherwise-owned result.
  5. Same-club submit/patch/delete all still work correctly.
  6. GET /league/faction-stats, GET /admin/history?scope=League, and
     GET /admin/league/results are all club-scoped — the other club's
     identically-factioned LeagueResult row never appears in a Manchester
     caller's results.
  7. GET /league/factions (list_factions, deliberately unscoped/public)
     still works without error.

All rows this script creates (the temp second club + its players +
LeagueResult row, temp Manchester players, temp Manchester LeagueResult
rows) are cleaned up in a `finally`, and _recalculate_ratings() is re-run
at the end to restore league_ratings to its pre-test state, leaving
staging exactly as it started.

Run with: python verify_scoped_helper_chunk3_league.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, LeagueResult, LeagueRating, Player, User
from main import app
import auth
from league import _recalculate_ratings


def _fake_super_admin(club_id: int, uid: int, player_id: int) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-scoped-chunk3-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=player_id,
        is_super_admin=True,
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
    created_result_ids = []

    try:
        # --- Second club + its own players, inserted directly (bypassing
        # the app), to prove scoping actually excludes them. ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk3 Other Club", slug="zztest-chunk3-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            other_p1 = Player(name="ZZTest Chunk3 Other P1", club_id=other_club_id, active=True)
            other_p2 = Player(name="ZZTest Chunk3 Other P2", club_id=other_club_id, active=True)
            db.add(other_p1)
            db.add(other_p2)
            db.commit()
            db.refresh(other_p1)
            db.refresh(other_p2)
            created_player_ids.extend([other_p1.id, other_p2.id])
            other_p1_id, other_p2_id = other_p1.id, other_p2.id
        print(f"Created other club id={other_club_id} with players id={other_p1_id},{other_p2_id}")

        # --- Manchester temp players for same-club tests ---
        with Session(engine) as db:
            m_p1 = Player(name="ZZTest Chunk3 Manchester P1", club_id=manchester_id, active=True)
            m_p2 = Player(name="ZZTest Chunk3 Manchester P2", club_id=manchester_id, active=True)
            db.add(m_p1)
            db.add(m_p2)
            db.commit()
            db.refresh(m_p1)
            db.refresh(m_p2)
            created_player_ids.extend([m_p1.id, m_p2.id])
            m_p1_id, m_p2_id = m_p1.id, m_p2.id
        print(f"Created Manchester players id={m_p1_id},{m_p2_id}")

        # A LeagueResult row belonging to the other club, inserted
        # directly, sharing the same faction as tests below use, so a
        # faction-stats leak would actually be observable.
        with Session(engine) as db:
            other_result = LeagueResult(
                player_1_id=other_p1_id,
                player_1_name="ZZTest Chunk3 Other P1",
                player_2_id=other_p2_id,
                player_2_name="ZZTest Chunk3 Other P2",
                result="Player 1 Victory",
                result_date="01/01/2099",
                player_1_faction="ZZTestFaction",
                player_2_faction="ZZTestFaction",
                game_type="Competitive",
                club_id=other_club_id,
            )
            db.add(other_result)
            db.commit()
            db.refresh(other_result)
            created_result_ids.append(other_result.id)
            other_result_id = other_result.id
        print(f"Created other club's LeagueResult id={other_result_id}")

        manchester_caller = _fake_super_admin(manchester_id, uid=999993, player_id=m_p1_id)
        app.dependency_overrides[auth.current_user] = lambda: manchester_caller
        app.dependency_overrides[auth.require_user] = lambda: manchester_caller

        # =====================================================================
        # 1. POST /league/results cross-club rejection (both directions)
        # =====================================================================
        resp = client.post("/league/results", json={
            "player_1_id": m_p1_id,
            "player_2_id": other_p2_id,
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        if resp.status_code != 404:
            problems.append(f"POST /league/results (p2 cross-club) expected 404, got {resp.status_code} {resp.text}")
        else:
            print("POST /league/results (Manchester p1, other-club p2) -> 404 (correct, rejected)")

        resp = client.post("/league/results", json={
            "player_1_id": other_p1_id,
            "player_2_id": m_p2_id,
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        if resp.status_code != 404:
            problems.append(f"POST /league/results (p1 cross-club) expected 404, got {resp.status_code} {resp.text}")
        else:
            print("POST /league/results (other-club p1, Manchester p2) -> 404 (correct, rejected)")

        # Confirm neither cross-club attempt actually created a row.
        with Session(engine) as db:
            leaked = db.exec(
                select(LeagueResult).where(
                    (LeagueResult.player_1_id == m_p1_id) | (LeagueResult.player_1_id == other_p1_id)
                ).where(LeagueResult.id != other_result_id)
            ).all()
            if leaked:
                problems.append(f"Cross-club submit_result calls unexpectedly created rows: {[r.id for r in leaked]}")
                created_result_ids.extend(r.id for r in leaked)

        # =====================================================================
        # 2. POST /league/results same-club success, club_id from user.club_id
        # =====================================================================
        resp = client.post("/league/results", json={
            "player_1_id": m_p1_id,
            "player_2_id": m_p2_id,
            "player_1_faction": "ZZTestFaction",
            "player_2_faction": "OtherFaction",
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        if resp.status_code != 200 or resp.json().get("duplicate") is not False:
            problems.append(f"POST /league/results (same-club) failed: {resp.status_code} {resp.text}")
            manchester_result_id = None
        else:
            manchester_result_id = resp.json()["result"]["id"]
            created_result_ids.append(manchester_result_id)
            with Session(engine) as db:
                row = db.get(LeagueResult, manchester_result_id)
                if row.club_id != manchester_id:
                    problems.append(f"Created LeagueResult club_id={row.club_id}, expected {manchester_id}")
                else:
                    print(f"POST /league/results (same-club) -> 200, created id={manchester_result_id}, club_id={row.club_id} (matches user.club_id)")

        # Duplicate-guard regression: resubmitting the identical result must
        # still be detected as a duplicate (no second row created).
        resp = client.post("/league/results", json={
            "player_1_id": m_p1_id,
            "player_2_id": m_p2_id,
            "player_1_faction": "ZZTestFaction",
            "player_2_faction": "OtherFaction",
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        if resp.status_code != 200 or resp.json().get("duplicate") is not True:
            problems.append(f"POST /league/results (duplicate resubmit) expected duplicate:true, got {resp.status_code} {resp.text}")
        else:
            print("POST /league/results (identical resubmit) -> duplicate:true (correct, dup-guard still works)")

        # =====================================================================
        # 3+4. Admin PATCH/DELETE cross-club rejection + patched-player check
        # =====================================================================
        resp = client.patch(f"/admin/league/results/{other_result_id}", json={"game_type": "Casual"})
        if resp.status_code != 404:
            problems.append(f"PATCH /admin/league/results/{{other club}} expected 404, got {resp.status_code} {resp.text}")
        else:
            print(f"PATCH /admin/league/results/{other_result_id} (other club's result) -> 404 (correct, rejected)")

        resp = client.delete(f"/admin/league/results/{other_result_id}")
        if resp.status_code != 404:
            problems.append(f"DELETE /admin/league/results/{{other club}} expected 404, got {resp.status_code} {resp.text}")
        else:
            print(f"DELETE /admin/league/results/{other_result_id} (other club's result) -> 404 (correct, rejected)")

        if manchester_result_id is not None:
            # Patching in a cross-club player id on an OWNED result must
            # also be rejected.
            resp = client.patch(f"/admin/league/results/{manchester_result_id}", json={"player_1_id": other_p1_id})
            if resp.status_code != 404:
                problems.append(f"PATCH /admin/league/results/{{owned}} with cross-club player_1_id expected 404, got {resp.status_code} {resp.text}")
            else:
                print(f"PATCH /admin/league/results/{manchester_result_id} (cross-club player_1_id) -> 404 (correct, rejected)")

            # Same-club patch must still work.
            resp = client.patch(f"/admin/league/results/{manchester_result_id}", json={"game_type": "Casual"})
            if resp.status_code != 200 or resp.json().get("game_type") != "Casual":
                problems.append(f"PATCH /admin/league/results/{{owned}} (same-club) failed: {resp.status_code} {resp.text}")
            else:
                print(f"PATCH /admin/league/results/{manchester_result_id} (same-club) -> 200, game_type updated correctly")

        # =====================================================================
        # 6. Read-side scoping: faction-stats, admin history, admin list
        # =====================================================================
        resp = client.get("/league/faction-stats", params={"faction": "ZZTestFaction"})
        if resp.status_code != 200:
            problems.append(f"GET /league/faction-stats failed: {resp.status_code} {resp.text}")
        else:
            names = {p["player_name"] for p in resp.json()["players"]}
            print(f"GET /league/faction-stats (faction=ZZTestFaction) player_names={names}")
            if "ZZTest Chunk3 Other P1" in names or "ZZTest Chunk3 Other P2" in names:
                problems.append("GET /league/faction-stats leaked the other club's players")
            if "ZZTest Chunk3 Manchester P1" not in names:
                problems.append("GET /league/faction-stats missing the caller's own club's player")

        resp = client.get("/admin/history", params={"scope": "League"})
        if resp.status_code != 200:
            problems.append(f"GET /admin/history?scope=League failed: {resp.status_code} {resp.text}")
        else:
            rows = resp.json()
            leaked = [r for r in rows if r["p1_name"].startswith("ZZTest Chunk3 Other")]
            if leaked:
                problems.append(f"GET /admin/history?scope=League leaked other club rows: {leaked}")
            own = [r for r in rows if r["p1_name"] == "ZZTest Chunk3 Manchester P1"]
            if not own:
                problems.append("GET /admin/history?scope=League missing the caller's own result")
            else:
                print(f"GET /admin/history?scope=League -> {len(rows)} rows, no other-club leak, own result present")

        resp = client.get("/admin/league/results")
        if resp.status_code != 200:
            problems.append(f"GET /admin/league/results failed: {resp.status_code} {resp.text}")
        else:
            rows = resp.json()
            leaked = [r for r in rows if r["player_1_name"].startswith("ZZTest Chunk3 Other")]
            if leaked:
                problems.append(f"GET /admin/league/results leaked other club rows: {leaked}")
            own = [r for r in rows if r["id"] == manchester_result_id]
            if not own:
                problems.append("GET /admin/league/results missing the caller's own result")
            else:
                print(f"GET /admin/league/results -> {len(rows)} rows, no other-club leak, own result present")

        # =====================================================================
        # 7. list_factions() — deliberately unscoped/public, just must not error
        # =====================================================================
        app.dependency_overrides.clear()
        resp = client.get("/league/factions")
        if resp.status_code != 200:
            problems.append(f"GET /league/factions (public, unscoped) failed: {resp.status_code} {resp.text}")
        else:
            print(f"GET /league/factions (public, unscoped) -> 200, factions={resp.json()['factions']}")

        # Re-apply override for the final same-club delete below.
        app.dependency_overrides[auth.current_user] = lambda: manchester_caller
        app.dependency_overrides[auth.require_user] = lambda: manchester_caller

        # Same-club delete must still work.
        if manchester_result_id is not None:
            resp = client.delete(f"/admin/league/results/{manchester_result_id}")
            if resp.status_code != 200:
                problems.append(f"DELETE /admin/league/results/{{owned}} (same-club) failed: {resp.status_code} {resp.text}")
            else:
                print(f"DELETE /admin/league/results/{manchester_result_id} (same-club) -> 200 (correct, deleted)")
                created_result_ids.remove(manchester_result_id)

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for rid in created_result_ids:
                r = db.get(LeagueResult, rid)
                if r:
                    db.delete(r)
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

            # Restore league_ratings to its pre-test state — every test
            # write above went through the global (unfiltered, known,
            # deliberately-not-fixed-here) recalc.
            _recalculate_ratings(db)
            db.commit()

            final_clubs = db.exec(select(Club)).all()
            final_results = db.exec(select(LeagueResult)).all()
            final_ratings = db.exec(select(LeagueRating)).all()
            print(
                f"Cleanup done: clubs={len(final_clubs)} (expect 1, Manchester only), "
                f"league_results={len(final_results)} (expect 0), "
                f"league_ratings={len(final_ratings)} (expect 0)"
            )

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print(
            "\nVerification passed: submit_result rejects cross-club players (404), "
            "admin PATCH/DELETE reject cross-club results and cross-club patched "
            "player ids (404), same-club submit/patch/delete all work, and "
            "faction-stats/admin-history/admin-list are all club-scoped with no leak."
        )


if __name__ == "__main__":
    main()
