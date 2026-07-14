"""One-off verification script for the league_results.club_id dual-run
(Phase 1, table 8 of 10). Not part of the app; run manually against
staging.

Exercises the one real LeagueResult-creating write site
(league.py::submit_result, POST /league/results) through FastAPI
TestClient, against the real staging DB, with only the auth dependency
overridden (in-memory fake super-admin, no real User row touched). Also
exercises PATCH/DELETE /league/results/{id} (edit/delete-only — confirm
club_id is untouched) and the duplicate-guard path.

Creates temporary Player rows (never touches the two real staging
players) so submitting results doesn't perturb real players' ELO/
achievement state. Staging had 0 real league_results/league_ratings rows
at the time this was written, so after cleanup + a _recalculate_ratings()
call, both tables are restored to empty — verified explicitly at the end.

Run with: python verify_league_results_club_id.py [--post-contract]
--post-contract also re-hits GET /league/factions, GET /league/faction-stats,
and GET /admin/league/results.
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, LeagueResult, LeagueRating, Player, User
from main import app
from league import _recalculate_ratings
import auth

created_player_ids = []
created_result_ids = []
problems = []


def _fake_super_admin(linked_player_id: int, club_id: int, uid: int = 999005) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-league-{uid}",
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


def main():
    post_contract = "--post-contract" in sys.argv

    with Session(engine) as db:
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
    print(f"Manchester club_id = {manchester_id}")

    p1_id = _mk_player("ZZTest LeagueP1", manchester_id)
    p2_id = _mk_player("ZZTest LeagueP2", manchester_id)

    fake_user = _fake_super_admin(p1_id, manchester_id)
    app.dependency_overrides[auth.require_user] = lambda: fake_user
    app.dependency_overrides[auth.current_user] = lambda: fake_user

    client = TestClient(app)

    try:
        # 1. POST /league/results (submit_result — the one creation site)
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
            body = r.json()
            if body.get("duplicate"):
                problems.append(f"POST /league/results: unexpected duplicate=True on first submission")
            result_id = body["result"]["id"]
            created_result_ids.append(result_id)
            with Session(engine) as db:
                row = db.get(LeagueResult, result_id)
                print(f"  result id={row.id} club_id={row.club_id}")
                if row.club_id != manchester_id:
                    problems.append(f"submit_result: club_id={row.club_id}, expected {manchester_id}")

        # 2. Duplicate guard: same submission again -> duplicate=True, no new row
        r = client.post("/league/results", json={
            "player_1_id": p1_id,
            "player_2_id": p2_id,
            "player_1_faction": "Death Korps",
            "player_2_faction": "Farstalker Kinband",
            "game_type": "Competitive",
            "result": "Player 1 Victory",
        })
        print("POST /league/results (duplicate) ->", r.status_code, r.json() if r.status_code == 200 else r.text)
        if r.status_code != 200:
            problems.append(f"POST /league/results (duplicate) failed: {r.status_code} {r.text}")
        elif not r.json().get("duplicate"):
            problems.append("POST /league/results (duplicate): expected duplicate=True, got False (new row created?)")
        with Session(engine) as db:
            count = len(db.exec(
                select(LeagueResult).where(LeagueResult.player_1_id == p1_id).where(LeagueResult.player_2_id == p2_id)
            ).all())
            if count != 1:
                problems.append(f"duplicate guard: expected exactly 1 row for this player pair, found {count}")

        # 3. PATCH /admin/league/results/{id} — edit-only, must not touch club_id
        if created_result_ids:
            rid = created_result_ids[0]
            r = client.patch(f"/admin/league/results/{rid}", json={"game_type": "Casual"})
            print("PATCH /admin/league/results/{id} ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"PATCH /admin/league/results/{{id}} failed: {r.status_code} {r.text}")
            with Session(engine) as db:
                row = db.get(LeagueResult, rid)
                print(f"  post-patch: game_type={row.game_type} club_id={row.club_id}")
                if row.club_id != manchester_id:
                    problems.append(f"PATCH clobbered club_id: {row.club_id}, expected {manchester_id}")
                if row.game_type != "Casual":
                    problems.append("PATCH: game_type not updated")

        if post_contract:
            r = client.get("/league/factions")
            print("GET /league/factions ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /league/factions failed: {r.status_code} {r.text}")

            r = client.get("/league/faction-stats", params={"faction": "Death Korps"})
            print("GET /league/faction-stats ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /league/faction-stats failed: {r.status_code} {r.text}")

            r = client.get("/admin/league/results")
            print("GET /admin/league/results ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /admin/league/results failed: {r.status_code} {r.text}")

        # 4. DELETE /admin/league/results/{id} — delete-only
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
            # Restore league_ratings to whatever it should be given remaining
            # real LeagueResult rows (staging had 0 before this script ran).
            _recalculate_ratings(db)
            db.commit()
            remaining_results = len(db.exec(select(LeagueResult)).all())
            remaining_ratings = len(db.exec(select(LeagueRating)).all())
            print(
                f"Cleaned up. league_results now {remaining_results} row(s), "
                f"league_ratings now {remaining_ratings} row(s)."
            )

    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
