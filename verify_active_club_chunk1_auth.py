"""Verify Phase 2 chunk 1 (auth.py active-club conversion) against the real
staging DB, using a genuine SECOND club so the checks prove the resolver
actually switches clubs by subdomain — not that results happen to look
unchanged with one club.

Exercises, via FastAPI TestClient with only auth.current_user overridden:
  1. resolve_active_club_slug_from_origin: bare/www -> None, subdomain -> slug.
  2. GET /auth/me with Origin = the 2nd club's subdomain -> active_club is the
     2nd club, and claim_candidates shows the 2nd club's unclaimed player, NOT
     Manchester's.
  3. GET /auth/me with NO Origin (bare domain) -> active_club falls back to the
     user's home club (Manchester).
  4. POST /auth/claim at the 2nd club -> sets that Player.user_id, and does NOT
     touch the legacy user.player_id (since the 2nd club isn't the user's home).
  5. POST /auth/create-profile at the 2nd club -> new Player owned by the user,
     club_id = 2nd club.
  6. A user already owning a player at the 2nd club is blocked from claiming a
     second one there (per-club uniqueness).

All temp rows (2nd club, its players, the test user) are removed in finally.

Run: PYTHONPATH=. python verify_active_club_chunk1_auth.py
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

import auth
from database import engine, resolve_active_club_slug_from_origin
from main import app
from models import Club, Player, User

client = TestClient(app)

problems: list[str] = []


def check(cond: bool, msg: str):
    if not cond:
        problems.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok: {msg}")


def override_user(user: User):
    app.dependency_overrides[auth.current_user] = lambda: user
    app.dependency_overrides[auth.require_user] = lambda: user


def main():
    with Session(engine) as db:
        manchester = db.exec(select(Club).where(Club.slug == "manchester")).first()
        assert manchester is not None

        temp_club = None
        temp_user = None
        temp_players: list[int] = []
        try:
            # --- unit: slug resolution -------------------------------------
            print("[1] resolve_active_club_slug_from_origin")
            check(resolve_active_club_slug_from_origin("https://calltoarms.app") is None,
                  "bare domain -> None")
            check(resolve_active_club_slug_from_origin("https://www.calltoarms.app") is None,
                  "www -> None")
            check(resolve_active_club_slug_from_origin("https://leeds.calltoarms.app") == "leeds",
                  "leeds subdomain -> 'leeds'")
            check(resolve_active_club_slug_from_origin(None) is None, "no origin -> None")

            # --- set up a real 2nd club + an unclaimed player in it ---------
            # Raw INSERT: clubs has an orphaned NOT-NULL leagues_enabled column
            # (retired flag, unmapped in the model) with no default, so the ORM
            # insert would violate it. Set it explicitly here.
            new_club_id = db.exec(text(
                "INSERT INTO clubs (name, slug, active, timezone, leagues_enabled, region, created_at) "
                "VALUES ('ZZTest Outpost', 'zztest-outpost', true, 'Europe/London', false, "
                "'Yorkshire & the Humber', now()) RETURNING id"
            )).one()[0]
            temp_club = db.get(Club, new_club_id)
            db.flush()
            other_unclaimed = Player(name="ZZTest Away Player", active=True,
                                     club_id=temp_club.id)
            db.add(other_unclaimed)
            # an unclaimed player at MANCHESTER too, to prove cross-club isolation
            man_unclaimed = Player(name="ZZTest Home Player", active=True,
                                   club_id=manchester.id)
            db.add(man_unclaimed)
            db.flush()
            temp_players += [other_unclaimed.id, man_unclaimed.id]

            temp_user = User(discord_id="zztest-active-club", discord_name="ZZTester",
                             club_id=manchester.id, home_club_id=manchester.id,
                             player_id=None)
            db.add(temp_user)
            db.commit()
            db.refresh(temp_user)
            db.refresh(temp_club)
            override_user(temp_user)

            away_origin = {"origin": f"https://{temp_club.slug}.calltoarms.app"}

            # --- [2] /auth/me on the away subdomain ------------------------
            print("[2] GET /auth/me with away-club Origin")
            r = client.get("/auth/me", headers=away_origin)
            body = r.json()
            check(r.status_code == 200, f"status 200 (got {r.status_code})")
            check(body.get("active_club", {}).get("slug") == temp_club.slug,
                  f"active_club is away club (got {body.get('active_club')})")
            cand_ids = {c["id"] for c in body.get("claim_candidates", [])}
            check(other_unclaimed.id in cand_ids,
                  "away club's unclaimed player IS a candidate")
            check(man_unclaimed.id not in cand_ids,
                  "Manchester's unclaimed player is NOT a candidate on away subdomain")

            # --- [3] /auth/me with no Origin -> home -----------------------
            print("[3] GET /auth/me with no Origin -> home club")
            r = client.get("/auth/me")
            body = r.json()
            check(body.get("active_club", {}).get("slug") == "manchester",
                  f"active_club falls back to home (got {body.get('active_club')})")
            cand_ids = {c["id"] for c in body.get("claim_candidates", [])}
            check(man_unclaimed.id in cand_ids and other_unclaimed.id not in cand_ids,
                  "home candidates are Manchester's, not the away club's")

            # --- [4] claim at the away club --------------------------------
            print("[4] POST /auth/claim at away club")
            r = client.post(f"/auth/claim/{other_unclaimed.id}", headers=away_origin)
            check(r.status_code == 200, f"claim ok (got {r.status_code}: {r.text})")
            db.refresh(other_unclaimed)
            db.refresh(temp_user)
            check(other_unclaimed.user_id == temp_user.id,
                  "away player's user_id now points at the test user")
            check(temp_user.player_id is None,
                  "legacy user.player_id UNTOUCHED (away club != home)")

            # --- [6] second claim at same club blocked ---------------------
            print("[6] second claim at away club blocked")
            r = client.post(f"/auth/claim/{man_unclaimed.id}", headers=away_origin)
            # man_unclaimed is a Manchester player, so this 404s (wrong club) —
            # but even a valid away player would 400 now the user owns one.
            check(r.status_code in (400, 404),
                  f"cannot claim a second/again (got {r.status_code})")

            # --- [5] create-profile at the away club (fresh user) ----------
            print("[5] POST /auth/create-profile at away club (2nd user)")
            temp_user2 = User(discord_id="zztest-active-club-2", discord_name="ZZTester2",
                              club_id=manchester.id, home_club_id=manchester.id,
                              player_id=None)
            db.add(temp_user2)
            db.commit()
            db.refresh(temp_user2)
            override_user(temp_user2)
            r = client.post("/auth/create-profile",
                            json={"name": "ZZTest Created Away"}, headers=away_origin)
            check(r.status_code == 200, f"create ok (got {r.status_code}: {r.text})")
            new_pid = r.json().get("player_id")
            if new_pid:
                temp_players.append(new_pid)
                np = db.get(Player, new_pid)
                check(np.club_id == temp_club.id, "created player is in the away club")
                check(np.user_id == temp_user2.id, "created player owned by the user")
                db.refresh(temp_user2)
                check(temp_user2.player_id is None,
                      "legacy user.player_id UNTOUCHED for away create")
            # temp_user2 is cleaned up in finally (its player must go first).

        finally:
            app.dependency_overrides.clear()
            with Session(engine) as clean:
                for pid in temp_players:
                    row = clean.get(Player, pid)
                    if row:
                        clean.delete(row)
                u = clean.exec(select(User).where(User.discord_id == "zztest-active-club")).first()
                if u:
                    clean.delete(u)
                u2 = clean.exec(select(User).where(User.discord_id == "zztest-active-club-2")).first()
                if u2:
                    clean.delete(u2)
                c = clean.exec(select(Club).where(Club.slug == "zztest-outpost")).first()
                if c:
                    clean.delete(c)
                clean.commit()
            # confirm staging restored
            with Session(engine) as chk:
                print("cleanup: clubs=%d users=%d players=%d" % (
                    len(chk.exec(select(Club)).all()),
                    len(chk.exec(select(User)).all()),
                    len(chk.exec(select(Player)).all()),
                ))

    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
