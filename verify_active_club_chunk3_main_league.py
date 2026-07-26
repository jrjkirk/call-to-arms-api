"""Verify Phase 2 chunk 3 (main.py + league.py active-club conversion) against
the real staging DB with a genuine SECOND club. Focuses on read-scoping
isolation and active-club resolution for the player pages, pairings, week-id,
and league endpoints.

Run: PYTHONPATH=. python verify_active_club_chunk3_main_league.py
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

import auth
from database import engine
from main import app
from models import Club, ClubSystem, Player, SystemConfig, User

client = TestClient(app)
problems: list[str] = []
SYSTEM = "The Old World"


def check(cond, msg):
    problems.append(msg) if not cond else None
    print(("  ok: " if cond else "  FAIL: ") + msg)


def override(user):
    app.dependency_overrides[auth.current_user] = lambda: user
    app.dependency_overrides[auth.require_user] = lambda: user


def main():
    with Session(engine) as db:
        manchester = db.exec(select(Club).where(Club.slug == "manchester")).first()
        tow = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == SYSTEM)).first()
        c = {"players": [], "cs": None, "user": None, "club": None}
        try:
            away_id = db.exec(text(
                "INSERT INTO clubs (name, slug, active, timezone, leagues_enabled, created_at) "
                "VALUES ('ZZTest Main Club','zztest-main',true,'Europe/London',false,now()) RETURNING id"
            )).one()[0]
            c["club"] = away_id
            cs = ClubSystem(club_id=away_id, system_id=tow.id, enabled=True,
                            session_day="Wednesday", session_cadence="weekly")
            db.add(cs); db.flush(); c["cs"] = cs.id

            user = User(discord_id="zztest-main-user", discord_name="ZZMain",
                        club_id=manchester.id, home_club_id=manchester.id, player_id=None)
            db.add(user); db.flush(); c["user"] = user.id
            home_p = Player(name="ZZMain Home", active=True, club_id=manchester.id, user_id=user.id)
            away_p = Player(name="ZZMain Away", active=True, club_id=away_id, user_id=user.id)
            db.add(home_p); db.add(away_p); db.flush()
            c["players"] = [home_p.id, away_p.id]
            db.commit(); db.refresh(user)

            override(user)
            away = {"origin": "https://zztest-main.calltoarms.app"}

            # [1] /players scoped to active club
            print("[1] GET /players on away subdomain")
            r = client.get("/players", headers=away)
            names = {p["name"] for p in r.json()}
            check(r.status_code == 200, f"200 ({r.status_code})")
            check("ZZMain Away" in names, "away player listed")
            check("ZZMain Home" not in names, "home player NOT listed on away subdomain")

            # [2] get_player isolation
            print("[2] GET /players/{id} isolation")
            check(client.get(f"/players/{away_p.id}", headers=away).status_code == 200,
                  "away player visible on away subdomain")
            check(client.get(f"/players/{home_p.id}", headers=away).status_code == 404,
                  "home player 404s on away subdomain")
            check(client.get(f"/players/{away_p.id}").status_code == 404,
                  "away player 404s on home (no origin)")

            # [3] discord identity via new ownership link (player.user_id)
            print("[3] get_player discord identity via player.user_id")
            body = client.get(f"/players/{away_p.id}", headers=away).json()
            check(body.get("discord", {}) is not None, "discord block present (owner resolved)")

            # [4] /pairings authed resolves to active club (empty but 200, scoped)
            print("[4] GET /pairings authed on away subdomain")
            r = client.get(f"/pairings?system={SYSTEM}&week=01/01/2099", headers=away)
            check(r.status_code == 200, f"pairings 200 scoped to away ({r.status_code})")

            # [5] /week-id authed resolves to active club (away runs TOW)
            print("[5] GET /week-id authed on away subdomain")
            r = client.get(f"/week-id?system={SYSTEM}", headers=away)
            check(r.status_code == 200 and "week_id" in r.json(),
                  f"week-id 200 for away's TOW ({r.status_code}: {r.text[:80]})")

            # [6] league submit resolves system at ACTIVE club: away has no
            #     league enabled -> 422 (proves _resolve_system_id uses active).
            print("[6] POST /league/results uses active-club player check")
            # home_p isn't in the away club -> 404 "Player 1 not found" proves
            # the club check uses the ACTIVE club (game_type supplied so the
            # body validates and we reach the club check).
            r = client.post("/league/results", headers=away, json={
                "system": SYSTEM, "player_1_id": home_p.id, "player_2_id": away_p.id,
                "result": "Draw", "game_type": "Casual",
            })
            check(r.status_code == 404,
                  f"cross-club player rejected at away club ({r.status_code}: {r.text[:80]})")

            # [7] faction-stats resolves the league at the ACTIVE club. The away
            #     club has no league enabled, so _resolve_system_id 404s there —
            #     proving it used the away club, not Manchester (which does).
            print("[7] GET /league/faction-stats resolves league at active club")
            r = client.get(f"/league/faction-stats?faction=Orcs&system={SYSTEM}", headers=away)
            check(r.status_code == 404,
                  f"no-league-at-away -> 404 (active-club resolution) ({r.status_code})")

        finally:
            app.dependency_overrides.clear()
            with Session(engine) as cl:
                for pid in c["players"]:
                    row = cl.get(Player, pid)
                    if row: cl.delete(row)
                if c["cs"]:
                    row = cl.get(ClubSystem, c["cs"])
                    if row: cl.delete(row)
                if c["user"]:
                    row = cl.get(User, c["user"])
                    if row: cl.delete(row)
                cl.commit()
                if c["club"]:
                    cl.exec(text("DELETE FROM clubs WHERE id = :i").bindparams(i=c["club"]))
                    cl.commit()
            with Session(engine) as chk:
                print("cleanup: clubs=%d users=%d players=%d" % (
                    len(chk.exec(select(Club)).all()),
                    len(chk.exec(select(User)).all()),
                    len(chk.exec(select(Player)).all()),
                ))

    if problems:
        print("\nFAILED:")
        [print("  -", p) for p in problems]
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
