"""In-app notices: the news a player who isn't on Discord would otherwise miss.

Joel asked (17/09/2026) for pairings and drop-outs to flash up on screen as a
closable box, rather than only reaching people through the club's Discord
channel. These are the checks on what gets written, to whom, and once.

What must hold:
  - publishing notifies every ACCOUNT playing that week, and nobody else
  - re-publishing the same week does not notify anyone twice
  - a drop-out from a published week tells both halves of every re-seated game
  - a drop-out from an UNPUBLISHED week tells nobody (nothing was public yet)
  - a late signup that gives a waiting player a game tells that player
  - guests and unlinked players are skipped, silently
  - a notice is one account's own: you cannot read or dismiss someone else's

Run: PYTHONPATH=. python tests/test_notices.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "notices.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import admin_signups  # noqa: E402
import auth  # noqa: E402
import database  # noqa: E402
import notices as notices_mod  # noqa: E402
import signups as signups_mod  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, Pairing, Player, PlayerNotice, PublishState, Signup,
    SystemConfig, User,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
WEEK = "23/09/2026"
H = {"origin": "https://egnwgc.calltoarms.app"}

signups_mod._post_webhook = lambda *a, **k: None
admin_signups._post_webhook = lambda *a, **k: None

SQLModel.metadata.create_all(database.engine)

# Dee has a Discord account, Gail a Google one, Pat a password one: as far as
# notices go they are identical, which is the point.
PLAYERS = [("Dee", 2), ("Gail", 3), ("Pat", 4), ("Wally", 5)]

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(Club(id=2, name="Other Club", slug="other", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
                        uses_points=True, default_points=2000, max_points=3000,
                        vibe_options=["Casual", "Competitive", "Open", "Intro"],
                        default_vibe="Casual"))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    db.add(User(id=1, discord_id="admin", discord_name="Admin", club_id=1,
                home_club_id=1, is_super_admin=True))
    for name, uid in PLAYERS:
        db.add(User(id=uid, display_name=name, club_id=1, home_club_id=1))
    db.add(Player(id=1, name="Dee", club_id=1, user_id=2, active=True))
    db.add(Player(id=2, name="Gail", club_id=1, user_id=3, active=True))
    db.add(Player(id=3, name="Pat", club_id=1, user_id=4, active=True))
    db.add(Player(id=4, name="Wally", club_id=1, user_id=5, active=True))
    # No account at all: an old roster row nobody has claimed.
    db.add(Player(id=5, name="Lonnie", club_id=1, user_id=None, active=True))
    # For the late-signup case: Vince is sitting out, Nate has not signed up yet.
    db.add(User(id=6, display_name="Vince", club_id=1, home_club_id=1))
    db.add(User(id=7, display_name="Nate", club_id=1, home_club_id=1))
    db.add(Player(id=6, name="Vince", club_id=1, user_id=6, active=True))
    db.add(Player(id=7, name="Nate", club_id=1, user_id=7, active=True))

    for sid, (pid, pname) in enumerate(
        [(1, "Dee"), (2, "Gail"), (3, "Pat"), (5, "Lonnie")], start=1
    ):
        db.add(Signup(id=sid, week=WEEK, system=TOW, player_id=pid, player_name=pname,
                      faction="Skaven", points=2000, eta="18:30", vibe="Casual",
                      club_id=1, created_at=datetime(2026, 9, 1, 12, sid)))
    # A guest: a real signup with no player row behind it.
    db.add(Signup(id=5, week=WEEK, system=TOW, player_id=None, player_name="Gary Guest",
                  faction="Empire of Man", points=2000, eta="18:30", vibe="Casual",
                  club_id=1, created_at=datetime(2026, 9, 1, 12, 9)))
    # Wally is sitting out with a BYE, so a drop-out can re-seat him.
    db.add(Signup(id=6, week=WEEK, system=TOW, player_id=4, player_name="Wally",
                  faction="Orc & Goblin Tribes", points=2000, eta="18:30", vibe="Casual",
                  club_id=1, created_at=datetime(2026, 9, 1, 12, 10)))

    db.add(Pairing(id=1, week=WEEK, system=TOW, a_signup_id=1, b_signup_id=2,
                   status="pending", club_id=1))
    db.add(Pairing(id=2, week=WEEK, system=TOW, a_signup_id=3, b_signup_id=5,
                   status="pending", club_id=1))
    db.add(Pairing(id=3, week=WEEK, system=TOW, a_signup_id=6, b_signup_id=None,
                   status="pending", club_id=1))
    db.commit()

ADMIN = TestClient(app)
ADMIN.cookies.set("cta_session", auth._make_session_cookie(1))


def client_for(user_id: int) -> TestClient:
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(user_id))
    return c


DEE, GAIL, PAT, WALLY = (client_for(u) for u in (2, 3, 4, 5))


def rows(**where):
    with Session(database.engine) as db:
        q = select(PlayerNotice)
        for k, v in where.items():
            q = q.where(getattr(PlayerNotice, k) == v)
        return db.exec(q.order_by(PlayerNotice.id)).all()


def mine(c: TestClient) -> list[dict]:
    r = c.get("/notices", headers=H)
    if r.status_code != 200:
        return [{"error": r.status_code, "body": r.text}]
    return r.json()["notices"]


def publish(state: bool):
    return ADMIN.post("/admin/pairings/publish", headers=H,
                      json={"system": TOW, "week": WEEK, "published": state})


print("\n=== the table is writable ===")
check("player_notices is in WRITE_ALLOWED_TABLES",
      "player_notices" in database.WRITE_ALLOWED_TABLES)

print("\n=== publishing tells the players in the week ===")
r = publish(True)
check("publish succeeded", r.status_code == 200, r.text)
written = rows(kind=notices_mod.PAIRINGS_PUBLISHED)
check("one notice per linked account in the week (Dee, Gail, Pat, Wally)",
      sorted(n.user_id for n in written) == [2, 3, 4, 5],
      [n.user_id for n in written])
check("the guest got nothing (no player row)",
      all(n.user_id is not None for n in written))
check("body names the system and the night",
      written and written[0].body == f"{TOW} pairings for Wed 23 Sep are up.",
      written[0].body if written else "none")
check("every notice starts unseen", all(n.seen_at is None for n in written))

print("\n=== re-publishing does not notify twice ===")
publish(False)
publish(True)
again = rows(kind=notices_mod.PAIRINGS_PUBLISHED)
check("still one each after unpublish + publish", len(again) == 4, len(again))

print("\n=== reading your own, and only your own ===")
dee = mine(DEE)
check("Dee sees her notice", len(dee) == 1 and dee[0]["kind"] == "pairings_published", dee)
check("the payload carries id/week/system/body",
      dee and {"id", "kind", "system", "week", "body", "created_at"} <= set(dee[0]), dee)
other = mine(GAIL)
check("Gail's notice is a different row", other and other[0]["id"] != dee[0]["id"])
dee_id = dee[0]["id"]
r = GAIL.post("/notices/seen", headers=H, json={"ids": [dee_id]})
check("Gail cannot dismiss Dee's notice", r.status_code == 200 and r.json()["seen"] == 0, r.text)
check("Dee's notice is still there", len(mine(DEE)) == 1)
r = DEE.post("/notices/seen", headers=H, json={"ids": [dee_id]})
check("Dee dismisses her own", r.status_code == 200 and r.json()["seen"] == 1, r.text)
check("and it does not come back", mine(DEE) == [])
r = DEE.post("/notices/seen", headers=H, json={"ids": [dee_id]})
check("dismissing twice is a no-op, not an error",
      r.status_code == 200 and r.json()["seen"] == 0, r.text)

print("\n=== signed out ===")
r = TestClient(app).get("/notices", headers=H)
check("anonymous gets 401, never someone else's notices", r.status_code == 401, r.status_code)

print("\n=== a stale notice is not shown ===")
with Session(database.engine) as db:
    n = db.exec(select(PlayerNotice).where(PlayerNotice.user_id == 3)).first()
    n.created_at = datetime.utcnow() - (notices_mod.SHOW_FOR + timedelta(days=1))
    db.add(n)
    db.commit()
check("a notice older than the show window is left behind", mine(GAIL) == [])

print("\n=== a drop-out from a published week ===")
# Gail drops. She was playing Dee, so Dee's night changes and Dee is told,
# whether the week re-seats her against someone waiting or leaves her on a BYE
# (which of the two is the matcher's business, not this feature's).
r = GAIL.delete("/signups/mine", params={"system": TOW, "week": WEEK}, headers=H)
check("Gail's drop succeeded", r.status_code == 200 and r.json().get("dropped"), r.text)
drops = rows(kind=notices_mod.OPPONENT_DROPPED)
told = {n.user_id: n.body for n in drops}
check("Dee, whose game changed, is told", 2 in told, told)
check("the notice names who dropped and the night",
      2 in told and "Gail" in told[2] and "Wed 23 Sep" in told[2], told.get(2))
check("Gail is not told about her own drop", 3 not in told, told.get(3))
check("Pat, whose game did not change, is not told", 4 not in told, told.get(4))
check("no None or doubled spaces in any body",
      all("None" not in b and "  " not in b for b in told.values()), told)
banner = mine(DEE)
check("and it is what Dee's banner now shows",
      len(banner) == 1 and banner[0]["kind"] == "opponent_dropped", banner)

print("\n=== both halves of a re-seated game are told ===")
# Driven straight through the writer, because which pairs the matcher makes is
# its own decision: what matters here is that when someone sitting out gains a
# game, they hear about it too, and that a guest with no account is skipped.
with Session(database.engine) as db:
    dee_su = db.exec(select(Signup).where(Signup.player_name == "Dee")
                     .where(Signup.week == WEEK)).first()
    wally_su = db.exec(select(Signup).where(Signup.player_name == "Wally")
                       .where(Signup.week == WEEK)).first()
    guest_su = db.exec(select(Signup).where(Signup.player_id == None)  # noqa: E711
                       .where(Signup.week == WEEK)).first()
    before = len(db.exec(select(PlayerNotice)).all())
    written = notices_mod.notify_opponent_dropped(
        db, 1, TOW, WEEK, [(dee_su, wally_su)], "Zelda"
    )
    db.commit()
    fresh = db.exec(select(PlayerNotice).order_by(PlayerNotice.id)).all()[before:]
check("two notices for one re-seated game", written == 2, written)
bodies = {n.user_id: n.body for n in fresh}
check("the player who lost an opponent is told who they play now",
      2 in bodies and "now playing Wally" in bodies[2], bodies.get(2))
check("the player who was sitting out is told they have a game",
      5 in bodies and "instead of sitting out" in bodies[5], bodies.get(5))
check("both name the dropper", all("Zelda" in b for b in bodies.values()), bodies)

with Session(database.engine) as db:
    dee_su = db.exec(select(Signup).where(Signup.player_name == "Dee")
                     .where(Signup.week == WEEK)).first()
    written = notices_mod.notify_opponent_dropped(
        db, 1, TOW, WEEK, [(dee_su, None)], "Zelda"
    )
    db.commit()
    latest = db.exec(select(PlayerNotice).order_by(PlayerNotice.id.desc())).first()
check("a new BYE is told they are without an opponent",
      written == 1 and "without an opponent" in latest.body, latest.body)

with Session(database.engine) as db:
    guest_su = db.exec(select(Signup).where(Signup.player_id == None)  # noqa: E711
                       .where(Signup.week == WEEK)).first()
    written = notices_mod.notify_opponent_dropped(
        db, 1, TOW, WEEK, [(guest_su, None)], "Zelda"
    )
    db.commit()
check("a guest, with no account, is skipped silently", written == 0, written)

with Session(database.engine) as db:
    lonnie_su = db.exec(select(Signup).where(Signup.player_name == "Lonnie")
                        .where(Signup.week == WEEK)).first()
    written = notices_mod.notify_opponent_dropped(
        db, 1, TOW, WEEK, [(lonnie_su, None)], "Zelda"
    )
    db.commit()
check("a player with no linked account is skipped silently", written == 0, written)

print("\n=== a drop-out from an unpublished week tells nobody ===")
OTHER_WEEK = "30/09/2026"
with Session(database.engine) as db:
    db.add(Signup(id=20, week=OTHER_WEEK, system=TOW, player_id=1, player_name="Dee",
                  faction="Skaven", points=2000, eta="18:30", vibe="Casual", club_id=1,
                  created_at=datetime(2026, 9, 8, 12, 0)))
    db.add(Signup(id=21, week=OTHER_WEEK, system=TOW, player_id=2, player_name="Gail",
                  faction="Empire of Man", points=2000, eta="18:30", vibe="Casual",
                  club_id=1, created_at=datetime(2026, 9, 8, 12, 1)))
    db.add(Pairing(id=20, week=OTHER_WEEK, system=TOW, a_signup_id=20, b_signup_id=21,
                   status="pending", club_id=1))
    db.commit()
before = len(rows(week=OTHER_WEEK))
r = DEE.delete("/signups/mine", params={"system": TOW, "week": OTHER_WEEK}, headers=H)
check("the unpublished drop succeeded", r.status_code == 200, r.text)
check("nothing was written for a week nobody had been shown",
      len(rows(week=OTHER_WEEK)) == before, rows(week=OTHER_WEEK))

print("\n=== a late signup that gives a waiting player a game ===")
# Vince is sitting out on a BYE in the published week. Nate signs up after
# pairings went out and lands opposite him, so Vince's night has changed.
with Session(database.engine) as db:
    db.add(Signup(id=30, week=WEEK, system=TOW, player_id=6, player_name="Vince",
                  faction="Dwarfen Mountain Holds", points=2000, eta="18:30",
                  vibe="Casual", club_id=1, created_at=datetime(2026, 9, 1, 12, 30)))
    db.add(Pairing(id=30, week=WEEK, system=TOW, a_signup_id=30, b_signup_id=None,
                   status="pending", club_id=1))
    db.commit()

r = ADMIN.post("/admin/signups", headers=H, json={
    "player_id": 7, "week": WEEK, "system": TOW, "faction": "Skaven",
    "points": 2000, "eta": "19:00", "vibe": "Casual",
})
check("Nate was added after pairings went out", r.status_code == 201, r.text)
with Session(database.engine) as db:
    seated_with = None
    row = db.exec(select(Pairing).where(Pairing.id == 30)).first()
    if row is not None and row.b_signup_id is not None:
        seated_with = db.get(Signup, row.b_signup_id)
check("Nate took the seat opposite Vince",
      seated_with is not None and seated_with.player_name == "Nate",
      seated_with.player_name if seated_with else None)
found = rows(kind=notices_mod.GAME_FOUND)
check("Vince was told he has a game after all",
      [n.user_id for n in found] == [6], [(n.user_id, n.body) for n in found])
check("the body names who joined and the night",
      found and "Nate" in found[0].body and "Wed 23 Sep" in found[0].body,
      found[0].body if found else None)
check("Nate, who already knows, is not told",
      all(n.user_id != 7 for n in rows(kind=notices_mod.GAME_FOUND)))

print("\n=== notices belong to one club ===")
with Session(database.engine) as db:
    db.add(PlayerNotice(user_id=2, club_id=2, system=TOW, week=WEEK,
                        kind=notices_mod.PAIRINGS_PUBLISHED, body="Other club's news."))
    db.commit()
check("a notice from another club is not shown here",
      all("Other club" not in n["body"] for n in mine(DEE)), mine(DEE))

print("\n=== week labels ===")
check("a real week reads as a day", notices_mod.week_label("23/09/2026") == "Wed 23 Sep")
check("an odd week string passes through", notices_mod.week_label("whenever") == "whenever")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("ALL PASS")
