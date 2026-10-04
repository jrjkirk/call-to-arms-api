"""League options a club picks when it sets a league up, and the guard against
one game being logged twice.

Damian asked (04/10/2026) for two things to be optional per league, ahead of
Warhammer 40,000 running as a league with no pairings: a single K value rather
than the casual/competitive pair, and painting bonuses that can be switched off
or set to whatever the club likes. Joel asked whether anything stops both
players logging the same game. Something did, but only if the two submissions
matched field for field, which they never do: each player's form puts
themselves down as Player 1.

What must hold:
  - a league that has touched none of this scores exactly as it did (10/40, +3/+1)
  - single K uses one value whatever game type a result carries
  - painting off adds nothing, and switching it back on restores the bonuses
  - the opponent logging the same game is caught, with the players swapped
  - a genuine second game goes through once confirmed
  - a result older than the window is not mistaken for a duplicate
  - the admin's log-from-pairings skips a game a player already logged

Run: PYTHONPATH=. python tests/test_league_config_options.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import date, datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "league_options.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import league as league_mod  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, LeagueRating, LeagueResult, LeagueSeason, Pairing, Player,
    Signup, SystemConfig, User,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


W40K = "Warhammer 40,000"
WEEK = "07/10/2026"

league_mod._post_league_webhook = lambda *a, **k: None
league_mod.announce_new_achievements = lambda *a, **k: None

SQLModel.metadata.create_all(database.engine)

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=W40K, slug="40k", legacy_system_name=W40K, active=True))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True, league_enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    db.add(LeagueSeason(id=1, club_id=1, system_id=1, name="Season 1", start_date=date(2026, 1, 1)))
    db.add(User(id=1, discord_id="admin", discord_name="Admin", club_id=1,
                home_club_id=1, is_super_admin=True))
    for pid, name in enumerate(["Ann", "Bob", "Cat", "Dan"], start=1):
        db.add(User(id=pid + 1, display_name=name, club_id=1, home_club_id=1))
        db.add(Player(id=pid, name=name, club_id=1, user_id=pid + 1, active=True))
    db.commit()


def client_for(user_id: int) -> TestClient:
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(user_id))
    return c


ADMIN, ANN, BOB = client_for(1), client_for(2), client_for(3)


def ratings() -> dict[str, float]:
    with Session(database.engine) as db:
        return {r.player_name: r.rating for r in db.exec(select(LeagueRating)).all()}


def results() -> list[LeagueResult]:
    with Session(database.engine) as db:
        return db.exec(select(LeagueResult).order_by(LeagueResult.id)).all()


def wipe():
    with Session(database.engine) as db:
        for model in (LeagueResult, LeagueRating):
            for row in db.exec(select(model)).all():
                db.delete(row)
        db.commit()


def save_config(**overrides):
    r = ADMIN.post("/admin/league-config", json={"system": W40K, **overrides})
    assert r.status_code == 200, r.text
    return r.json()


def submit(client, p1, p2, result="Player 1 Victory", **extra):
    r = client.post("/league/results", json={
        "player_1_id": p1, "player_2_id": p2, "result": result, "system": W40K, **extra,
    })
    assert r.status_code == 200, r.text
    return r.json()


print("Defaults: a league that changed nothing scores as it always did")
cfg = ADMIN.get("/admin/league-config", params={"system": W40K}).json()
check("single K is off by default", cfg["single_k"] is False)
check("the suggested single K is 32", cfg["k_single"] == 32)
check("painting is on by default", cfg["painting_enabled"] is True)
form = ANN.get("/league/config", params={"system": W40K}).json()
check("the result form asks for a game type", form["uses_game_type"] is True)
check("the result form asks for painting", form["painting_enabled"] is True)

submit(ANN, 1, 2, game_type="Casual", player_1_painting_bonus="Fully Painted")
# Equal ratings, so the winner gains K/2: 10/2 = 5, plus 3 for fully painted.
check("casual win at K=10 with a fully painted army", ratings() == {"Ann": 1008.0, "Bob": 995.0}, str(ratings()))
check("the K used is recorded", results()[0].k_factor_used == 10)
wipe()
submit(ANN, 1, 2, game_type="Competitive")
check("competitive win at K=40", ratings() == {"Ann": 1020.0, "Bob": 980.0}, str(ratings()))

print("Single K: one value, whatever the result says its game type was")
save_config(single_k=True, k_single=24)
check("saving replays the season under the new K", ratings() == {"Ann": 1012.0, "Bob": 988.0}, str(ratings()))
check("the K used follows", results()[0].k_factor_used == 24)
form = ANN.get("/league/config", params={"system": W40K}).json()
check("the result form stops asking for a game type", form["uses_game_type"] is False)
wipe()
submit(ANN, 1, 2)  # no game_type at all, as the single-K form sends it
check("a result with no game type is accepted and scored", ratings() == {"Ann": 1012.0, "Bob": 988.0}, str(ratings()))
wipe()
submit(ANN, 1, 2, game_type="Casual")
check("a casual result still uses the single K", ratings() == {"Ann": 1012.0, "Bob": 988.0}, str(ratings()))

print("Painting: any values, or none at all")
wipe()
save_config(single_k=True, k_single=24, painting_fully_bonus=10, painting_partial_bonus=4.5)
submit(ANN, 1, 2, player_1_painting_bonus="Fully Painted", player_2_painting_bonus="Partially Painted")
check("custom bonuses apply", ratings() == {"Ann": 1022.0, "Bob": 992.5}, str(ratings()))
save_config(single_k=True, k_single=24, painting_enabled=False, painting_fully_bonus=10, painting_partial_bonus=4.5)
check("switching painting off removes them", ratings() == {"Ann": 1012.0, "Bob": 988.0}, str(ratings()))
check("the result keeps what was entered", results()[0].player_1_painting_bonus == "Fully Painted")
form = ANN.get("/league/config", params={"system": W40K}).json()
check("the result form stops asking for painting", form["painting_enabled"] is False)
save_config(single_k=True, k_single=24, painting_enabled=True, painting_fully_bonus=10, painting_partial_bonus=4.5)
check("switching it back on restores them", ratings() == {"Ann": 1022.0, "Bob": 992.5}, str(ratings()))
wipe()
save_config(single_k=True, k_single=24, painting_enabled=False)
submit(ANN, 1, 2, player_1_painting_bonus="Fully Painted")
check("painting sent to a league without it is not stored", results()[0].player_1_painting_bonus is None)

print("Painting under flat win/loss points")
wipe()
save_config(scoring_method="winloss", starting_rating=0, painting_enabled=True, painting_fully_bonus=2)
submit(ANN, 1, 2, player_2_painting_bonus="Fully Painted")
check("win/loss adds painting when the league uses it", ratings() == {"Ann": 3.0, "Bob": 2.0}, str(ratings()))
save_config(scoring_method="winloss", starting_rating=0, painting_enabled=False)
check("and not when it doesn't", ratings() == {"Ann": 3.0, "Bob": 0.0}, str(ratings()))
form = ANN.get("/league/config", params={"system": W40K}).json()
check("win/loss never asks for a game type", form["uses_game_type"] is False)

print("One game, logged once")
wipe()
save_config()
first = submit(ANN, 1, 2, "Player 1 Victory")
check("Ann logs her win over Bob", first["duplicate"] is False)
# Bob's form puts Bob down as Player 1, so the same game arrives the other way
# round. This is the case the old field-for-field guard let through.
second = submit(BOB, 2, 1, "Player 2 Victory")
check("Bob logging the same game is caught", second["duplicate"] is True)
check("Bob is told what is already logged", second["existing"]["outcome"] == "Ann beat Bob", str(second.get("existing")))
check("only one result exists", len(results()) == 1)
disagree = submit(BOB, 2, 1, "Player 1 Victory", player_1_faction="Orks")
check("caught even when the details differ", disagree["duplicate"] is True and len(results()) == 1)
again = submit(BOB, 2, 1, "Player 1 Victory", confirm_second_game=True)
check("a confirmed second game goes through", again["duplicate"] is False and len(results()) == 2)
other = submit(ANN, 1, 3)
check("a game against someone else is not a duplicate", other["duplicate"] is False)

print("An older result is not a duplicate")
wipe()
submit(ANN, 1, 2)
with Session(database.engine) as db:
    row = db.exec(select(LeagueResult)).one()
    row.created_at = datetime.utcnow() - league_mod.DUPLICATE_WINDOW - timedelta(hours=1)
    db.add(row)
    db.commit()
rematch = submit(BOB, 2, 1)
check("next week's rematch needs no confirmation", rematch["duplicate"] is False and len(results()) == 2)

print("Admin log-from-pairings skips a game a player already logged")
wipe()
with Session(database.engine) as db:
    db.add(Signup(id=1, week=WEEK, system=W40K, player_id=1, player_name="Ann", club_id=1))
    db.add(Signup(id=2, week=WEEK, system=W40K, player_id=2, player_name="Bob", club_id=1))
    db.add(Signup(id=3, week=WEEK, system=W40K, player_id=3, player_name="Cat", club_id=1))
    db.add(Signup(id=4, week=WEEK, system=W40K, player_id=4, player_name="Dan", club_id=1))
    db.add(Pairing(id=1, week=WEEK, system=W40K, a_signup_id=1, b_signup_id=2, status="pending", club_id=1))
    db.add(Pairing(id=2, week=WEEK, system=W40K, a_signup_id=3, b_signup_id=4, status="pending", club_id=1))
    db.commit()
submit(BOB, 2, 1, "Player 1 Victory")  # Bob logs it first, as Player 1
r = ADMIN.post("/admin/league/results/from-pairings", json={"system": W40K, "week": WEEK, "results": [
    {"pairing_id": 1, "result": "Player 2 Victory"},
    {"pairing_id": 2, "result": "Draw"},
]})
body = r.json()
check("the bulk log succeeds", r.status_code == 200, r.text)
check("the already-logged game is skipped", body.get("skipped") == [{"pairing_id": 1, "reason": "already logged"}], str(body))
check("the other game is created", body.get("created") == 1 and len(results()) == 2)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("All passed.")
