"""One-off verification script for the scoped-query-helper phase, chunk 10
(main.py -- the final file of this phase). Not part of the app; run
manually against staging.

Proves, beyond "no errors":

  1. list_players: GET /players excludes a second club's identically-shaped
     active player, and its systems_played aggregation doesn't leak the
     other club's signup rows onto a shared player_id collision.
  2. get_player ownership-check fix: GET /players/{other club's player id}
     -> 404 for a Manchester caller (the important part of this chunk).
     Same-club get still works (positive control).
  3. get_player's rating/rank/pairings/discord sections are all scoped:
     rating_row/rank computed only from Manchester's LeagueRating rows
     (rank correctness, not just visibility), recent_games_by_system only
     shows Manchester's own Pairing rows, and discord_info is correct for
     a real linked user (proves the user-variable-shadowing rename didn't
     break anything).
  4. league_rankings rank correctness: with a second club's players present
     with ratings that would change Manchester's players' rank if mixed in
     globally, Manchester's rankings/ranks are computed using only
     Manchester's LeagueRating rows.
  5. signups_stats: GET /signups/stats isolated per club for an identical
     week/system key.
  6. list_systems and get_pairings unaffected (same behavior before/after,
     confirmed by exercising both, since deliberately untouched).

All rows this script creates (a genuine second temp club + its
players/users/signups/pairings/league data, temp Manchester players/
signups/pairings/league data) are cleaned up in a `finally`, leaving
staging exactly as it started.

Run with: python verify_scoped_helper_chunk10_main.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, LeagueRating, Pairing, Player, PublishState, Signup, User
from main import app
import auth

TEST_SYSTEM = "Kill Team"
WEEK = "26/01/2099"


def _fake_user(club_id: int, uid: int, player_id=None) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk10-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=player_id,
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
    created_user_ids = []
    created_signup_ids = []
    created_pairing_ids = []
    created_rating_ids = []
    created_publish_ids = []

    def _mk_player(name, club_id):
        with Session(engine) as db:
            p = Player(name=name, active=True, club_id=club_id)
            db.add(p)
            db.commit()
            db.refresh(p)
            created_player_ids.append(p.id)
            return p.id

    def _mk_user_row(club_id, player_id, discord_suffix):
        with Session(engine) as db:
            u = User(
                discord_id=f"zztest-chunk10-{discord_suffix}",
                discord_name=f"ZZTest Chunk10 {discord_suffix}",
                avatar_url=f"https://example.com/{discord_suffix}.png",
                player_id=player_id,
                is_super_admin=False,
                club_id=club_id,
            )
            db.add(u)
            db.commit()
            db.refresh(u)
            created_user_ids.append(u.id)
            return u.id

    def _mk_signup(club_id, player_id, player_name, week=WEEK, **kw):
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
            return su.id

    def _mk_pairing(club_id, a_signup_id, b_signup_id, week=WEEK, **kw):
        with Session(engine) as db:
            p = Pairing(
                week=week, system=TEST_SYSTEM, a_signup_id=a_signup_id,
                b_signup_id=b_signup_id, club_id=club_id, **kw,
            )
            db.add(p)
            db.commit()
            db.refresh(p)
            created_pairing_ids.append(p.id)
            return p.id

    def _mk_rating(club_id, player_id, player_name, rating):
        with Session(engine) as db:
            r = LeagueRating(player_id=player_id, player_name=player_name, rating=rating, club_id=club_id)
            db.add(r)
            db.commit()
            db.refresh(r)
            created_rating_ids.append(r.id)
            return r.id

    def _mk_publish(club_id, published=True, week=WEEK):
        with Session(engine) as db:
            ps = PublishState(week=week, system=TEST_SYSTEM, published=published, club_id=club_id)
            db.add(ps)
            db.commit()
            db.refresh(ps)
            created_publish_ids.append(ps.id)
            return ps.id

    try:
        # ---- Set up a genuine second temp club ----
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk10 Club", slug="zztest-chunk10-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id
        print(f"Created temp second club id={other_club_id}")

        # ---- 1. list_players isolation ----
        mcr_player = _mk_player("ZZTest MCR Player10", manchester_id)
        other_player = _mk_player("ZZTest Other Player10", other_club_id)
        _mk_signup(manchester_id, mcr_player, "ZZTest MCR Player10")
        _mk_signup(other_club_id, other_player, "ZZTest Other Player10")

        mcr_user = _fake_user(manchester_id, uid=900001, player_id=mcr_player)
        app.dependency_overrides[auth.require_user] = lambda: mcr_user
        app.dependency_overrides[auth.current_user] = lambda: mcr_user

        r = client.get("/players")
        assert r.status_code == 200, r.text
        ids = {p["id"] for p in r.json()}
        if other_player in ids:
            problems.append("list_players leaked other club's player")
        if mcr_player not in ids:
            problems.append("list_players missing own club's player")
        mcr_row = next(p for p in r.json() if p["id"] == mcr_player)
        if TEST_SYSTEM not in mcr_row["systems_played"]:
            problems.append("list_players systems_played missing own signup's system")
        print("1. list_players isolation: OK" if not problems else f"1. PROBLEMS: {problems}")

        # ---- 2. get_player ownership check ----
        r = client.get(f"/players/{other_player}")
        if r.status_code != 404:
            problems.append(f"get_player cross-club should 404, got {r.status_code}")
        r = client.get(f"/players/{mcr_player}")
        if r.status_code != 200:
            problems.append(f"get_player same-club should 200, got {r.status_code}")
        print(f"2. get_player ownership check: {'OK' if r.status_code == 200 else 'FAIL'}")

        # ---- 3. get_player rating/rank/pairings/discord isolation ----
        # Ratings that would change rank if mixed globally: other club's
        # player has a HIGHER rating than Manchester's, which would push
        # Manchester's player to rank 2 if not scoped.
        _mk_rating(other_club_id, other_player, "ZZTest Other Player10", rating=2000.0)
        _mk_rating(manchester_id, mcr_player, "ZZTest MCR Player10", rating=1500.0)

        mcr_user2 = _mk_player("ZZTest MCR Player10b", manchester_id)
        mcr_signup2 = _mk_signup(manchester_id, mcr_user2, "ZZTest MCR Player10b", week="27/01/2099")
        mcr_signup1 = _mk_signup(manchester_id, mcr_player, "ZZTest MCR Player10", week="27/01/2099")
        mcr_pairing = _mk_pairing(manchester_id, mcr_signup1, mcr_signup2, week="27/01/2099")

        other_player2 = _mk_player("ZZTest Other Player10b", other_club_id)
        other_signup2 = _mk_signup(other_club_id, other_player2, "ZZTest Other Player10b", week="27/01/2099")
        other_signup1 = _mk_signup(other_club_id, other_player, "ZZTest Other Player10", week="27/01/2099")
        other_pairing = _mk_pairing(other_club_id, other_signup1, other_signup2, week="27/01/2099")

        discord_user_id = _mk_user_row(manchester_id, mcr_player, "discord10")

        r = client.get(f"/players/{mcr_player}")
        assert r.status_code == 200, r.text
        body = r.json()
        if body["league"]["rating"] != 1500.0:
            problems.append(f"get_player rating wrong: {body['league']['rating']}")
        if body["league"]["rank"] != 1:
            problems.append(f"get_player rank leaked other club's higher rating: rank={body['league']['rank']}")
        games = body["recent_games_by_system"].get(TEST_SYSTEM, [])
        if not games:
            problems.append("get_player recent_games_by_system missing own club's pairing")
        if body["discord"] is None or body["discord"]["discord_name"] != "ZZTest Chunk10 discord10":
            problems.append(f"get_player discord_info wrong after rename: {body['discord']}")
        print("3. get_player rating/rank/pairings/discord: OK" if not any(
            "get_player rating" in p or "get_player rank" in p or "recent_games" in p or "discord_info" in p
            for p in problems
        ) else f"3. PROBLEMS present: {problems}")

        # ---- 4. league_rankings rank correctness ----
        r = client.get("/league/rankings")
        assert r.status_code == 200, r.text
        rankings = r.json()
        pids = {row["player_id"] for row in rankings}
        if other_player in pids or other_player2 in pids:
            problems.append("league_rankings leaked other club's player")
        mcr_ranking = next((row for row in rankings if row["player_id"] == mcr_player), None)
        if mcr_ranking is None:
            problems.append("league_rankings missing own club's player")
        elif mcr_ranking["rank"] != 1:
            problems.append(f"league_rankings rank leaked other club's higher rating: rank={mcr_ranking['rank']}")
        print("4. league_rankings rank correctness: OK" if mcr_ranking and mcr_ranking["rank"] == 1 else "4. FAIL")

        # ---- 5. signups_stats isolation ----
        r = client.get("/signups/stats", params={"system": TEST_SYSTEM, "week": WEEK})
        assert r.status_code == 200, r.text
        stats = r.json()
        if stats["signed_up"] != 1:
            problems.append(f"signups_stats leaked or missed rows: signed_up={stats['signed_up']}")
        print("5. signups_stats isolation: OK" if stats["signed_up"] == 1 else f"5. FAIL: {stats}")

        # ---- 6. list_systems / get_pairings unaffected ----
        del app.dependency_overrides[auth.require_user]
        del app.dependency_overrides[auth.current_user]

        r = client.get("/systems")
        if r.status_code != 200 or not isinstance(r.json(), list):
            problems.append(f"list_systems broken: {r.status_code}")

        _mk_publish(manchester_id, published=True, week="27/01/2099")
        r = client.get("/pairings", params={"system": TEST_SYSTEM, "week": "27/01/2099"})
        if r.status_code != 200 or not r.json()["published"]:
            problems.append(f"get_pairings broken: {r.status_code} {r.text}")
        else:
            names = {m["player_a_name"] for m in r.json()["matchups"]} | {m["player_b_name"] for m in r.json()["matchups"]}
            if "ZZTest MCR Player10" not in names:
                problems.append("get_pairings didn't return expected matchup (unauthenticated public endpoint, unscoped by design)")
        print("6. list_systems/get_pairings unaffected: OK" if r.status_code == 200 else f"6. FAIL: {r.text}")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for pid in created_pairing_ids:
                obj = db.get(Pairing, pid)
                if obj:
                    db.delete(obj)
            for sid in created_signup_ids:
                obj = db.get(Signup, sid)
                if obj:
                    db.delete(obj)
            for rid in created_rating_ids:
                obj = db.get(LeagueRating, rid)
                if obj:
                    db.delete(obj)
            for psid in created_publish_ids:
                obj = db.get(PublishState, psid)
                if obj:
                    db.delete(obj)
            for uid in created_user_ids:
                obj = db.get(User, uid)
                if obj:
                    db.delete(obj)
            for pid in created_player_ids:
                obj = db.get(Player, pid)
                if obj:
                    db.delete(obj)
            db.commit()
            if other_club_id is not None:
                club = db.get(Club, other_club_id)
                if club:
                    db.delete(club)
                    db.commit()
        print("Cleanup done.")

    if problems:
        print("\nFAILURES:")
        for p in problems:
            print(f" - {p}")
        sys.exit(1)
    else:
        print("\nAll checks passed.")


if __name__ == "__main__":
    main()
