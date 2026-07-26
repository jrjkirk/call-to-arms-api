"""Verify Phase 2 chunk 2 (signups.py active-club conversion) against the real
staging DB with a genuine SECOND club. Proves the core travelling-player loop:
a user signs up / views / drops AT the club whose subdomain they're on, and it
never crosses with their home club.

Run: PYTHONPATH=. python verify_active_club_chunk2_signups.py
"""
import sys

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

import auth
from database import engine
from main import app
from models import Club, ClubSystem, Player, Signup, SystemConfig, User

client = TestClient(app)
problems: list[str] = []
WEEK = "01/01/2099"          # far-future week so it can't collide with real data
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
        assert manchester and tow

        created = {"players": [], "signups": [], "club_system": None, "user": None, "club": None}
        try:
            # away club + enable TOW there
            away_id = db.exec(text(
                "INSERT INTO clubs (name, slug, active, timezone, leagues_enabled, created_at) "
                "VALUES ('ZZTest Signups Club','zztest-signups',true,'Europe/London',false,now()) RETURNING id"
            )).one()[0]
            created["club"] = away_id
            cs = ClubSystem(club_id=away_id, system_id=tow.id, enabled=True,
                            session_day="Wednesday", session_cadence="weekly")
            db.add(cs); db.flush(); created["club_system"] = cs.id

            user = User(discord_id="zztest-signups-user", discord_name="ZZSignups",
                        club_id=manchester.id, home_club_id=manchester.id, player_id=None)
            db.add(user); db.flush(); created["user"] = user.id

            # this user owns a player at BOTH clubs
            home_p = Player(name="ZZSignups Home", active=True, club_id=manchester.id, user_id=user.id)
            away_p = Player(name="ZZSignups Away", active=True, club_id=away_id, user_id=user.id)
            db.add(home_p); db.add(away_p); db.flush()
            created["players"] = [home_p.id, away_p.id]
            db.commit()
            db.refresh(user)

            override(user)
            away_origin = {"origin": "https://zztest-signups.calltoarms.app"}

            # [1] submit at the AWAY club
            print("[1] POST /signups on away subdomain")
            r = client.post("/signups", json={"system": SYSTEM, "week": WEEK, "faction": "Orcs"},
                            headers=away_origin)
            check(r.status_code == 200, f"submit ok ({r.status_code}: {r.text[:120]})")
            su = r.json().get("signup", {})
            if su.get("id"):
                created["signups"].append(su["id"])
            check(su.get("club_id") == away_id, f"signup.club_id == away ({su.get('club_id')})")
            check(su.get("player_id") == away_p.id, "signup bound to the AWAY player, not home")

            # [2] GET /signups/mine on away -> sees it
            print("[2] GET /signups/mine on away subdomain")
            r = client.get(f"/signups/mine?system={SYSTEM}&week={WEEK}", headers=away_origin)
            check(r.status_code == 200 and r.json().get("current") is not None,
                  "away 'current' present")

            # [3] GET /signups/mine on HOME (no origin) -> does NOT see the away signup
            print("[3] GET /signups/mine on home (no origin) -> isolated")
            r = client.get(f"/signups/mine?system={SYSTEM}&week={WEEK}")
            # home player has no signup this week; away signup must not leak in
            cur = r.json().get("current")
            check(cur is None, f"home 'current' is None, away signup did not leak ({cur})")

            # [4] submit at HOME too, confirm two independent rows
            print("[4] POST /signups on home -> independent row")
            r = client.post("/signups", json={"system": SYSTEM, "week": WEEK, "faction": "Empire"})
            check(r.status_code == 200, f"home submit ok ({r.status_code}: {r.text[:120]})")
            hsu = r.json().get("signup", {})
            if hsu.get("id"):
                created["signups"].append(hsu["id"])
            check(hsu.get("club_id") == manchester.id and hsu.get("player_id") == home_p.id,
                  "home signup bound to home club+player")
            check(hsu.get("id") != su.get("id"), "home and away signups are distinct rows")

            # [5] drop at away only -> home survives
            print("[5] DELETE /signups/mine on away -> home survives")
            r = client.delete(f"/signups/mine?system={SYSTEM}&week={WEEK}", headers=away_origin)
            check(r.status_code == 200 and r.json().get("dropped") is True, "away drop ok")
            still = db.exec(select(Signup).where(Signup.id == hsu["id"])).first()
            check(still is not None, "home signup still present after away drop")
            gone = db.exec(select(Signup).where(Signup.id == su["id"])).first()
            check(gone is None, "away signup removed")

        finally:
            app.dependency_overrides.clear()
            with Session(engine) as c:
                for sid in created["signups"]:
                    row = c.get(Signup, sid)
                    if row: c.delete(row)
                c.commit()
                for pid in created["players"]:
                    row = c.get(Player, pid)
                    if row: c.delete(row)
                if created["club_system"]:
                    row = c.get(ClubSystem, created["club_system"])
                    if row: c.delete(row)
                if created["user"]:
                    row = c.get(User, created["user"])
                    if row: c.delete(row)
                c.commit()
                if created["club"]:
                    c.exec(text("DELETE FROM clubs WHERE id = :i").bindparams(i=created["club"]))
                    c.commit()
            with Session(engine) as chk:
                print("cleanup: clubs=%d users=%d players=%d signups(week)=%d" % (
                    len(chk.exec(select(Club)).all()),
                    len(chk.exec(select(User)).all()),
                    len(chk.exec(select(Player)).all()),
                    len(chk.exec(select(Signup).where(Signup.week == WEEK)).all()),
                ))

    if problems:
        print("\nFAILED:")
        [print("  -", p) for p in problems]
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
