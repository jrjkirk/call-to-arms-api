"""One-off verification script for the league_ratings.club_id dual-run
(Phase 1, table 9 of 10). Not part of the app; run manually against
staging.

league_ratings has exactly one write path: league.py::_recalculate_ratings,
called from submit_result (POST /league/results), and admin.py's
PATCH/DELETE /admin/league/results/{id}. Every call fully replaces the
table's contents, so this exercises all three callers and checks the
resulting LeagueRating rows' club_id, then confirms cleanup restores both
league_results and league_ratings to their pre-test row counts.

Creates temporary Player rows (never touches the two real staging
players) so submitting results doesn't perturb real players' ELO/
achievement state. Staging had 0 real league_results/league_ratings rows
at the time this was written.

Run with: python verify_league_ratings_club_id.py [--post-contract]
--post-contract also re-hits GET /league/rankings, GET /players/{id}
(rank display) in addition to the three write-triggering endpoints.
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, LeagueResult, LeagueRating, Player, User
from main import app
import auth

created_player_ids = []
created_result_ids = []
problems = []


def _fake_super_admin(linked_player_id: int, club_id: int, uid: int = 999006) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-ratings-{uid}",
        discord_name="Verify Script",
        player_id=linked_player_id,
        is_super_admin=True,
        club_id=club_id,
    )


def _mk_player(name, manchester_id):
    with Session(engine) as db:
        p = Player(name=name, active=True, club_id=manchester_id)
        db.add(p)
        db.commit()
        db.refresh(p)
        created_player_ids.append(p.id)
        return p.id


def _ratings_club_ids():
    with Session(engine) as db:
        rows = db.exec(select(LeagueRating)).all()
        return {r.player_id: r.club_id for r in rows}


def main():
    post_contract = "--post-contract" in sys.argv

    with Session(engine) as db:
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
        results_before = len(db.exec(select(LeagueResult)).all())
        ratings_before = len(db.exec(select(LeagueRating)).all())
    print(f"Manchester club_id = {manchester_id}")
    print(f"Starting counts: league_results={results_before}, league_ratings={ratings_before}")

    p1_id = _mk_player("ZZTest RatingP1", manchester_id)
    p2_id = _mk_player("ZZTest RatingP2", manchester_id)

    fake_user = _fake_super_admin(p1_id, manchester_id)
    app.dependency_overrides[auth.require_user] = lambda: fake_user
    app.dependency_overrides[auth.current_user] = lambda: fake_user

    client = TestClient(app)

    try:
        # 1. POST /league/results -> triggers _recalculate_ratings via submit_result
        r = client.post("/league/results", json={
            "player_1_id": p1_id,
            "player_2_id": p2_id,
            "player_1_faction": "Death Korps",
            "player_2_faction": "Farstalker Kinband",
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        print("POST /league/results ->", r.status_code)
        if r.status_code != 200:
            problems.append(f"POST /league/results failed: {r.status_code} {r.text}")
        else:
            result_id = r.json()["result"]["id"]
            created_result_ids.append(result_id)

        club_ids = _ratings_club_ids()
        print(f"  post-submit league_ratings club_ids: {club_ids}")
        for pid in (p1_id, p2_id):
            if club_ids.get(pid) != manchester_id:
                problems.append(f"LeagueRating for player {pid}: club_id={club_ids.get(pid)}, expected {manchester_id}")

        # 2. PATCH /admin/league/results/{id} -> also triggers _recalculate_ratings
        if created_result_ids:
            rid = created_result_ids[0]
            r = client.patch(f"/admin/league/results/{rid}", json={"game_type": "Casual"})
            print("PATCH /admin/league/results/{id} ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"PATCH /admin/league/results/{{id}} failed: {r.status_code} {r.text}")
            club_ids = _ratings_club_ids()
            print(f"  post-patch league_ratings club_ids: {club_ids}")
            for pid in (p1_id, p2_id):
                if club_ids.get(pid) != manchester_id:
                    problems.append(f"post-PATCH LeagueRating for player {pid}: club_id={club_ids.get(pid)}, expected {manchester_id}")

        if post_contract:
            r = client.get("/league/rankings")
            print("GET /league/rankings ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /league/rankings failed: {r.status_code} {r.text}")

            r = client.get(f"/players/{p1_id}")
            print(f"GET /players/{p1_id} ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /players/{{id}} failed: {r.status_code} {r.text}")
            elif r.json().get("league", {}).get("rank") is None:
                problems.append("GET /players/{id}: expected a rank to be set after ratings recalc")

        # 3. DELETE /admin/league/results/{id} -> also triggers _recalculate_ratings
        if created_result_ids:
            rid = created_result_ids[0]
            r = client.delete(f"/admin/league/results/{rid}")
            print("DELETE /admin/league/results/{id} ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"DELETE /admin/league/results/{{id}} failed: {r.status_code} {r.text}")
            else:
                created_result_ids.remove(rid)

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for rid in created_result_ids:
                row = db.get(LeagueResult, rid)
                if row:
                    db.delete(row)
            for pid in created_player_ids:
                pl = db.get(Player, pid)
                if pl:
                    db.delete(pl)
            db.flush()
            from league import _recalculate_ratings
            _recalculate_ratings(db)
            db.commit()
            results_after = len(db.exec(select(LeagueResult)).all())
            ratings_after = len(db.exec(select(LeagueRating)).all())
            print(f"Cleaned up. league_results now {results_after} row(s), league_ratings now {ratings_after} row(s).")
            if results_after != results_before:
                problems.append(f"league_results not restored: {results_after} != {results_before}")
            if ratings_after != ratings_before:
                problems.append(f"league_ratings not restored: {ratings_after} != {ratings_before}")

    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
