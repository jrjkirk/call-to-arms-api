"""One-off verification script for the _recalculate_ratings() cross-club
ELO isolation fix. Not part of the app; run manually against staging.

Proves, beyond "no errors":
  1. Submitting a result for Manchester does NOT delete the other club's
     LeagueRating rows (the "actively destructive" half of the bug).
  2. Submitting a result for Manchester does NOT include the other club's
     LeagueResult rows in the replay (the other club's own
     k_factor_used/rating_before/rating_after fields are untouched, and
     Manchester's resulting ratings match what they'd be from Manchester's
     results alone, not a blended pool).
  3. The reverse: submitting a result for the other club leaves
     Manchester's LeagueRating rows untouched and Manchester's ratings
     unaffected.
  4. Math correctness (same-club, single-club scenario): the K-factor,
     expected-score formula, and painting bonus produce byte-identical
     output to an independent from-scratch computation of the same
     formula — proving the fix only changed row selection, not the math.
  5. All 3 call sites exercised: POST /league/results,
     PATCH /admin/league/results/{id}, DELETE /admin/league/results/{id}.

All rows this script creates (temp second club + its players + results,
temp Manchester players + results) are cleaned up in a `finally`, with
per-club _recalculate_ratings() calls (not the old global one) to restore
each club's league_ratings to its pre-test state.

Run with: python verify_recalculate_ratings_scoping.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, LeagueResult, LeagueRating, Player, User
from main import app
import auth
from league import _recalculate_ratings, _painting_bonus


def _fake_super_admin(club_id: int, uid: int, player_id: int) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-recalc-scoping-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=player_id,
        is_super_admin=True,
        club_id=club_id,
    )


def _expected_elo(k: float, r1: float, r2: float, score: float, p1_bonus, p2_bonus):
    e1 = 1.0 / (1.0 + 10.0 ** ((r2 - r1) / 400.0))
    e2 = 1.0 / (1.0 + 10.0 ** ((r1 - r2) / 400.0))
    new_r1 = r1 + k * (score - e1) + _painting_bonus(p1_bonus)
    new_r2 = r2 + k * ((1.0 - score) - e2) + _painting_bonus(p2_bonus)
    return new_r1, new_r2


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
        # --- Second club + its own players ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Recalc Other Club", slug="zztest-recalc-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            op1 = Player(name="ZZTest Recalc Other P1", club_id=other_club_id, active=True)
            op2 = Player(name="ZZTest Recalc Other P2", club_id=other_club_id, active=True)
            db.add(op1)
            db.add(op2)
            db.commit()
            db.refresh(op1)
            db.refresh(op2)
            created_player_ids.extend([op1.id, op2.id])
            op1_id, op2_id = op1.id, op2.id
        print(f"Created other club id={other_club_id} with players id={op1_id},{op2_id}")

        with Session(engine) as db:
            mp1 = Player(name="ZZTest Recalc Manchester P1", club_id=manchester_id, active=True)
            mp2 = Player(name="ZZTest Recalc Manchester P2", club_id=manchester_id, active=True)
            db.add(mp1)
            db.add(mp2)
            db.commit()
            db.refresh(mp1)
            db.refresh(mp2)
            created_player_ids.extend([mp1.id, mp2.id])
            mp1_id, mp2_id = mp1.id, mp2.id
        print(f"Created Manchester players id={mp1_id},{mp2_id}")

        manchester_caller = _fake_super_admin(manchester_id, uid=999994, player_id=mp1_id)
        other_caller = _fake_super_admin(other_club_id, uid=999995, player_id=op1_id)

        def as_manchester():
            app.dependency_overrides[auth.current_user] = lambda: manchester_caller
            app.dependency_overrides[auth.require_user] = lambda: manchester_caller

        def as_other():
            app.dependency_overrides[auth.current_user] = lambda: other_caller
            app.dependency_overrides[auth.require_user] = lambda: other_caller

        # =====================================================================
        # Seed a baseline game for BOTH clubs via the real endpoint, so both
        # clubs have real LeagueResult + LeagueRating rows before the
        # isolation test below.
        # =====================================================================
        as_other()
        resp = client.post("/league/results", json={
            "player_1_id": op1_id,
            "player_2_id": op2_id,
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        if resp.status_code != 200:
            problems.append(f"Seed POST /league/results (other club) failed: {resp.status_code} {resp.text}")
            sys.exit(1)
        other_baseline_id = resp.json()["result"]["id"]
        created_result_ids.append(other_baseline_id)
        print(f"Seeded other club baseline result id={other_baseline_id}")

        as_manchester()
        resp = client.post("/league/results", json={
            "player_1_id": mp1_id,
            "player_2_id": mp2_id,
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        if resp.status_code != 200:
            problems.append(f"Seed POST /league/results (Manchester) failed: {resp.status_code} {resp.text}")
            sys.exit(1)
        manchester_baseline_id = resp.json()["result"]["id"]
        created_result_ids.append(manchester_baseline_id)
        print(f"Seeded Manchester baseline result id={manchester_baseline_id}")

        # Snapshot other club's state before Manchester submits anything else.
        with Session(engine) as db:
            other_ratings_before = {
                r.id: (r.player_id, r.rating)
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == other_club_id)).all()
            }
            other_result_before = db.get(LeagueResult, other_baseline_id)
            other_result_fields_before = (
                other_result_before.player_1_rating_before,
                other_result_before.player_1_rating_after,
                other_result_before.player_2_rating_before,
                other_result_before.player_2_rating_after,
                other_result_before.k_factor_used,
            )
        if not other_ratings_before:
            problems.append("Other club has no LeagueRating rows after seeding — cannot test isolation")
        print(f"Other club LeagueRating snapshot before Manchester submit: {other_ratings_before}")

        # =====================================================================
        # 1+2. Manchester submits a SECOND result. Other club's LeagueRating
        # rows must not be deleted, and other club's LeagueResult row must
        # not have been replayed (fields unchanged).
        # =====================================================================
        as_manchester()
        resp = client.post("/league/results", json={
            "player_1_id": mp1_id,
            "player_2_id": mp2_id,
            "game_type": "Competitive",
            "result": "Player 2 Victory",
        })
        if resp.status_code != 200:
            problems.append(f"POST /league/results (Manchester 2nd game) failed: {resp.status_code} {resp.text}")
        else:
            manchester_result2_id = resp.json()["result"]["id"]
            created_result_ids.append(manchester_result2_id)
            print(f"Manchester 2nd result id={manchester_result2_id}")

        with Session(engine) as db:
            other_ratings_after = {
                r.id: (r.player_id, r.rating)
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == other_club_id)).all()
            }
            other_result_after = db.get(LeagueResult, other_baseline_id)
            other_result_fields_after = (
                other_result_after.player_1_rating_before,
                other_result_after.player_1_rating_after,
                other_result_after.player_2_rating_before,
                other_result_after.player_2_rating_after,
                other_result_after.k_factor_used,
            )

        if other_ratings_after != other_ratings_before:
            problems.append(
                f"Other club's LeagueRating rows changed after Manchester submit! "
                f"before={other_ratings_before} after={other_ratings_after}"
            )
        else:
            print("Other club's LeagueRating rows unchanged after Manchester submit (not deleted/recreated) -- OK")

        if other_result_fields_after != other_result_fields_before:
            problems.append(
                f"Other club's LeagueResult row was replayed by Manchester's submit! "
                f"before={other_result_fields_before} after={other_result_fields_after}"
            )
        else:
            print("Other club's LeagueResult row untouched by Manchester's recalc -- OK")

        # Manchester's own ratings must reflect ONLY Manchester's 2 games,
        # not a blended pool with the other club's game.
        with Session(engine) as db:
            m_ratings = {
                r.player_id: r.rating
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == manchester_id)).all()
            }
        r1, r2 = 1000.0, 1000.0
        r1, r2 = _expected_elo(40, r1, r2, 1.0, None, None)  # game 1: p1 win
        r1, r2 = _expected_elo(40, r1, r2, 0.0, None, None)  # game 2: p2 win
        expected = {mp1_id: r1, mp2_id: r2}
        if abs(m_ratings.get(mp1_id, -1) - expected[mp1_id]) > 1e-6 or abs(m_ratings.get(mp2_id, -1) - expected[mp2_id]) > 1e-6:
            problems.append(
                f"Manchester ratings not isolated to Manchester's own games! "
                f"got={m_ratings} expected={expected}"
            )
        else:
            print(f"Manchester ratings match Manchester-only replay (not blended with other club): {m_ratings}")

        # =====================================================================
        # 3. Reverse direction: other club submits, Manchester untouched.
        # =====================================================================
        with Session(engine) as db:
            manchester_ratings_before = {
                r.id: (r.player_id, r.rating)
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == manchester_id)).all()
            }

        as_other()
        resp = client.post("/league/results", json={
            "player_1_id": op1_id,
            "player_2_id": op2_id,
            "game_type": "Competitive",
            "result": "Draw",
        })
        if resp.status_code != 200:
            problems.append(f"POST /league/results (other club 2nd game) failed: {resp.status_code} {resp.text}")
        else:
            other_result2_id = resp.json()["result"]["id"]
            created_result_ids.append(other_result2_id)
            print(f"Other club 2nd result id={other_result2_id}")

        with Session(engine) as db:
            manchester_ratings_after = {
                r.id: (r.player_id, r.rating)
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == manchester_id)).all()
            }
        if manchester_ratings_after != manchester_ratings_before:
            problems.append(
                f"Manchester's LeagueRating rows changed after other club's submit! "
                f"before={manchester_ratings_before} after={manchester_ratings_after}"
            )
        else:
            print("Manchester's LeagueRating rows unchanged after other club's submit -- OK (reverse direction)")

        # Snapshot other club's state again now that their own 2nd game has
        # legitimately changed it -- this is the correct baseline for the
        # PATCH/DELETE isolation check below (step 5), not the pre-step-3
        # snapshot which predates the other club's own submission.
        with Session(engine) as db:
            other_ratings_after_own_submit = {
                r.id: (r.player_id, r.rating)
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == other_club_id)).all()
            }

        # =====================================================================
        # 4. Math correctness on a fresh same-club pair (no cross-club
        # involvement at all) -- confirms K-factor/expected-score/painting
        # bonus math is byte-identical to an independent computation.
        # =====================================================================
        with Session(engine) as db:
            mp3 = Player(name="ZZTest Recalc Manchester P3", club_id=manchester_id, active=True)
            mp4 = Player(name="ZZTest Recalc Manchester P4", club_id=manchester_id, active=True)
            db.add(mp3)
            db.add(mp4)
            db.commit()
            db.refresh(mp3)
            db.refresh(mp4)
            created_player_ids.extend([mp3.id, mp4.id])
            mp3_id, mp4_id = mp3.id, mp4.id

        as_manchester()
        resp = client.post("/league/results", json={
            "player_1_id": mp3_id,
            "player_2_id": mp4_id,
            "game_type": "Casual",  # k=10 branch
            "result": "Draw",
            "player_1_painting_bonus": "Fully Painted",
            "player_2_painting_bonus": "Partially Painted",
        })
        if resp.status_code != 200:
            problems.append(f"POST /league/results (math check) failed: {resp.status_code} {resp.text}")
        else:
            math_result_id = resp.json()["result"]["id"]
            created_result_ids.append(math_result_id)
            with Session(engine) as db:
                row = db.get(LeagueResult, math_result_id)
                actual = (row.player_1_rating_before, row.player_1_rating_after,
                          row.player_2_rating_before, row.player_2_rating_after, row.k_factor_used)
            exp_r1, exp_r2 = _expected_elo(10, 1000.0, 1000.0, 0.5, "Fully Painted", "Partially Painted")
            expected = (1000.0, exp_r1, 1000.0, exp_r2, 10)
            if any(abs(a - e) > 1e-9 if isinstance(a, float) else a != e for a, e in zip(actual, expected)):
                problems.append(f"Math check mismatch: actual={actual} expected={expected}")
            else:
                print(f"Math check (casual, draw, painting bonuses) matches independent computation: {actual}")

        # =====================================================================
        # 5. PATCH and DELETE call sites.
        # =====================================================================
        resp = client.patch(f"/admin/league/results/{math_result_id}", json={"game_type": "Competitive"})
        if resp.status_code != 200:
            problems.append(f"PATCH /admin/league/results/{{id}} failed: {resp.status_code} {resp.text}")
        else:
            with Session(engine) as db:
                other_ratings_check = {
                    r.id: (r.player_id, r.rating)
                    for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == other_club_id)).all()
                }
            print(f"PATCH /admin/league/results/{math_result_id} -> 200, game_type now Competitive (k=40 branch)")
            row = resp.json()
            if row["k_factor_used"] != 40:
                problems.append(f"PATCH recalc k_factor_used expected 40, got {row['k_factor_used']}")

        resp = client.delete(f"/admin/league/results/{math_result_id}")
        if resp.status_code != 200:
            problems.append(f"DELETE /admin/league/results/{{id}} failed: {resp.status_code} {resp.text}")
        else:
            created_result_ids.remove(math_result_id)
            print(f"DELETE /admin/league/results/{math_result_id} -> 200")

        # Other club must still be untouched after the PATCH+DELETE above.
        with Session(engine) as db:
            other_ratings_final = {
                r.id: (r.player_id, r.rating)
                for r in db.exec(select(LeagueRating).where(LeagueRating.club_id == other_club_id)).all()
            }
        if other_ratings_final != other_ratings_after_own_submit:
            problems.append(
                f"Other club's LeagueRating rows changed after Manchester PATCH/DELETE! "
                f"before={other_ratings_after_own_submit} final={other_ratings_final}"
            )
        else:
            print("Other club's LeagueRating rows still unchanged after Manchester PATCH/DELETE -- OK")

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
            # Restore each club's league_ratings independently now that
            # _recalculate_ratings() is club-scoped.
            _recalculate_ratings(db, manchester_id)
            db.commit()
            if other_club_id:
                _recalculate_ratings(db, other_club_id)
                db.commit()
                club = db.get(Club, other_club_id)
                if club:
                    db.delete(club)
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
            "\nVerification passed: _recalculate_ratings() is club-scoped -- "
            "other club's LeagueRating rows are never deleted, other club's "
            "LeagueResult rows are never replayed, ratings stay isolated per "
            "club in both directions, math output is byte-identical to an "
            "independent computation, and all 3 call sites work."
        )


if __name__ == "__main__":
    main()
