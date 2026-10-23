"""GET /home: the "Your week" tab's one call.

What must hold:
  - every system the club runs appears, with its next session's week id
  - the count is one per player plus every guest, as /signups/stats counts
  - "mine" is true only for the caller's own signups
  - "published" follows PublishState
  - a league shows only where the caller is ranked, with W/D/L and a level
  - another club's signups are never counted

Run: PYTHONPATH=. python tests/test_home.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import date, datetime

_DB = pathlib.Path(tempfile.mkdtemp()) / "home.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, LeagueRating, LeagueResult, LeagueSeason, Player, PublishState,
    Signup, SystemConfig, User,
)
from week_logic import next_session_date  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW, KT = "The Old World", "Kill Team"
TOW_WEEK = next_session_date("Wednesday", "weekly", None, date.today()).strftime("%d/%m/%Y")
KT_WEEK = next_session_date("Friday", "weekly", None, date.today()).strftime("%d/%m/%Y")

SQLModel.metadata.create_all(database.engine)

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(Club(id=2, name="Other Club", slug="other", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True))
    db.add(SystemConfig(id=2, name=KT, slug="kt", legacy_system_name=KT, active=True))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, session_day="Wednesday",
                      session_cadence="weekly", accent_color="#ee6868", league_enabled=True))
    db.add(ClubSystem(id=2, club_id=1, system_id=2, enabled=True, session_day="Friday",
                      session_cadence="weekly"))
    db.add(ClubSystem(id=3, club_id=2, system_id=1, enabled=True, session_day="Wednesday",
                      session_cadence="weekly"))
    db.add(User(id=1, display_name="Dee", club_id=1, home_club_id=1))
    db.add(User(id=2, display_name="Gail", club_id=1, home_club_id=1))
    db.add(Player(id=1, name="Dee", club_id=1, user_id=1, active=True))
    db.add(Player(id=2, name="Gail", club_id=1, user_id=2, active=True))
    db.add(Player(id=3, name="Elsewhere", club_id=2, active=True))
    for sid, (pid, name) in enumerate([(1, "Dee"), (2, "Gail"), (None, "Guest A"), (None, "Guest B")], 1):
        db.add(Signup(id=sid, week=TOW_WEEK, system=TOW, player_id=pid, player_name=name,
                      club_id=1, created_at=datetime(2026, 9, 1, 12, sid)))
    # Dee edited their signup: a second row for the same player counts once.
    db.add(Signup(id=5, week=TOW_WEEK, system=TOW, player_id=1, player_name="Dee",
                  club_id=1, created_at=datetime(2026, 9, 1, 13, 0)))
    db.add(Signup(id=6, week=KT_WEEK, system=KT, player_id=2, player_name="Gail",
                  club_id=1, created_at=datetime(2026, 9, 1, 12, 0)))
    db.add(Signup(id=7, week=TOW_WEEK, system=TOW, player_id=3, player_name="Elsewhere",
                  club_id=2, created_at=datetime(2026, 9, 1, 12, 0)))
    db.add(PublishState(week=TOW_WEEK, system=TOW, published=True, club_id=1))
    db.add(LeagueSeason(id=1, club_id=1, system_id=1, name="S1", start_date=date(2020, 1, 1)))
    db.add(LeagueRating(player_id=1, player_name="Dee", rating=1248, club_id=1, system_id=1, season_id=1))
    db.add(LeagueRating(player_id=2, player_name="Gail", rating=1300, club_id=1, system_id=1, season_id=1))
    db.add(LeagueResult(player_1_id=1, player_1_name="Dee", player_2_id=2, player_2_name="Gail", result="Player 1 Victory",
                        club_id=1, system_id=1, season_id=1, result_date="2026-09-02"))
    db.add(LeagueResult(player_1_id=1, player_1_name="Dee", player_2_id=2, player_2_name="Gail", result="Draw",
                        club_id=1, system_id=1, season_id=1, result_date="2026-09-09"))
    db.commit()


def client_for(user_id):
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(user_id))
    return c


r = client_for(1).get("/home", headers={"origin": "https://egnwgc.calltoarms.app"})
check("200 for a member", r.status_code == 200, r.text)
body = r.json()
nights = {n["system"]: n for n in body["nights"]}
check("both systems the club runs", set(nights) == {TOW, KT}, nights.keys())
tow, kt = nights[TOW], nights[KT]
check("week id is the next session", tow["week"] == TOW_WEEK and kt["week"] == KT_WEEK)
check("one per player, every guest, no other club", tow["signed_up"] == 4, tow["signed_up"])
check("mine where the caller signed up", tow["mine"] is True and kt["mine"] is False)
check("published follows PublishState", tow["published"] is True and kt["published"] is False)
check("accent colour, with gold by default", tow["accent_color"] == "#ee6868" and kt["accent_color"] == "#c9a14a")
check("sorted by date", [n["date"] for n in body["nights"]] == sorted(n["date"] for n in body["nights"]))

leagues = body["leagues"]
check("one league, where the caller is ranked", len(leagues) == 1 and leagues[0]["system"] == TOW, leagues)
lg = leagues[0]
check("rank and field size", lg["rank"] == 2 and lg["of"] == 2, lg)
check("W/D/L", (lg["wins"], lg["draws"], lg["losses"]) == (1, 1, 0), lg)
check("a level is included", isinstance(lg["level"], int) and "level_percent" in lg, lg)

r2 = client_for(2).get("/home", headers={"origin": "https://egnwgc.calltoarms.app"})
check("Gail sees Kill Team as theirs", {n["system"]: n["mine"] for n in r2.json()["nights"]}[KT] is True)

check("signed out is 401", TestClient(app).get("/home").status_code == 401)

# GET /players: the list page sorts by last_played and shows usual_faction.
with Session(database.engine) as db:
    db.add(Signup(id=20, week="02/09/2026", system=TOW, player_id=2, player_name="Gail",
                  faction="Skaven", club_id=1, created_at=datetime(2026, 9, 1, 12, 0)))
    db.add(Signup(id=21, week="09/09/2026", system=TOW, player_id=2, player_name="Gail",
                  faction="Skaven", club_id=1, created_at=datetime(2026, 9, 8, 12, 0)))
    db.add(Signup(id=22, week="31/12/2099", system=KT, player_id=2, player_name="Gail",
                  faction="Kommandos", club_id=1, created_at=datetime(2026, 9, 8, 12, 0)))
    db.commit()
rows = {p["name"]: p for p in client_for(1).get("/players", headers={"origin": "https://egnwgc.calltoarms.app"}).json()}
gail = rows["Gail"]
check("last_played is the latest session already held, not a future signup",
      gail["last_played"] is not None and gail["last_played"] < "2099-01-01", gail["last_played"])
check("usual faction is the most-played (system, faction)",
      (gail["usual_system"], gail["usual_faction"]) == (TOW, "Skaven"), gail)
check("no faction signed up: no usual faction", rows["Dee"]["usual_faction"] is None, rows["Dee"])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("All passed.")
