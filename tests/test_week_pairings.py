"""A paired week keeps every signup in exactly one pairing row.

The sweep after the 16/09 standby incident found players who fell out of a
week's pairings with nobody told: a drop left a generated game pointing at a
deleted signup, a late signup got no row at all, an admin grid delete stranded
both players, and a drop created a second BYE beside someone already sitting
out. It also found Preview disagreeing with Generate, a "no BYE twice running"
guard that did nothing, admin-added veterans shown as New, and experience
moving the moment Generate was pressed.

After every scenario `whole()` checks the rule directly: every signup in the
week sits in exactly one row, and no row points at a signup that is gone.

Run: PYTHONPATH=. python tests/test_week_pairings.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "week_pairings.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import admin  # noqa: E402
import auth  # noqa: E402
import database  # noqa: E402
import signups  # noqa: E402
from experience import counts_for_players  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, Pairing, PairingBlock, Player, PublishState, Signup,
    SystemConfig, User,
)
from pairings_engine import generate, previous_bye_player_ids  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
WEEK = "23/09/2026"
LAST = "16/09/2026"

POSTS: list[str] = []


def fake_post(db, club_id, system, content, *args, **kwargs):
    POSTS.append(content)


signups._post_webhook = fake_post
admin._post_webhook = fake_post

SQLModel.metadata.create_all(database.engine)
NAMES = ["Ann", "Bob", "Cat", "Dan", "Eve", "Fay", "Gus", "Hal"]
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(
        id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
        uses_points=True, default_points=2000, max_points=3000,
        allows_demo=True, uses_standby=True,
        vibe_options=["Casual", "Competitive", "Open", "Intro"], default_vibe="Casual",
    ))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    # User 1 is a super admin with no player; users 2.. each own one player.
    db.add(User(id=1, discord_id="admin", discord_name="Admin", club_id=1,
                home_club_id=1, is_super_admin=True))
    for i, name in enumerate(NAMES, start=1):
        db.add(User(id=i + 1, discord_id=f"d{i}", discord_name=name, club_id=1, home_club_id=1))
        db.add(Player(id=i, name=name, club_id=1, user_id=i + 1, active=True))
    db.commit()

PID = {name: i for i, name in enumerate(NAMES, start=1)}


def client_for(user_id):
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(user_id))
    return c


ADMIN = client_for(1)


def as_player(name):
    return client_for(PID[name] + 1)


def reset():
    POSTS.clear()
    with Session(database.engine) as db:
        for model in (Pairing, Signup, PairingBlock, PublishState):
            for row in db.exec(select(model)).all():
                db.delete(row)
        db.commit()


def signup(name, week=WEEK, vibe="Casual", standby=False, minutes=0):
    with Session(database.engine) as db:
        su = Signup(week=week, system=TOW, player_id=PID[name], player_name=name,
                    faction="Skaven", points=2000, eta="18:00", vibe=vibe,
                    standby_ok=standby, club_id=1,
                    created_at=datetime(2026, 9, 1, 12, 0) + timedelta(minutes=minutes or PID[name]))
        db.add(su)
        db.commit()
        db.refresh(su)
        return su.id


def row(a, b=None, week=WEEK, prearranged=False):
    """A pairing between two signup ids (b None for a BYE)."""
    with Session(database.engine) as db:
        p = Pairing(week=week, system=TOW, a_signup_id=a, b_signup_id=b, status="pending",
                    prearranged=prearranged, club_id=1)
        db.add(p)
        db.commit()
        db.refresh(p)
        return p.id


def publish(week=WEEK):
    with Session(database.engine) as db:
        db.add(PublishState(week=week, system=TOW, published=True, club_id=1))
        db.commit()


def state(week=WEEK):
    """Sorted list of games as frozensets of names, and sorted BYE names."""
    with Session(database.engine) as db:
        sus = {s.id: s.player_name for s in db.exec(select(Signup).where(Signup.week == week)).all()}
        games, byes = [], []
        for p in db.exec(select(Pairing).where(Pairing.week == week)).all():
            if p.b_signup_id is None:
                byes.append(sus.get(p.a_signup_id, f"#{p.a_signup_id}"))
            else:
                games.append(frozenset({sus.get(p.a_signup_id, f"#{p.a_signup_id}"),
                                        sus.get(p.b_signup_id, f"#{p.b_signup_id}")}))
        return games, sorted(byes)


def whole(label, week=WEEK):
    with Session(database.engine) as db:
        su_ids = {s.id for s in db.exec(select(Signup).where(Signup.week == week)).all()}
        rows = db.exec(select(Pairing).where(Pairing.week == week)).all()
        refs = [sid for p in rows for sid in (p.a_signup_id, p.b_signup_id) if sid is not None]
        ghosts = [sid for sid in refs if sid not in su_ids]
        counts = {sid: refs.count(sid) for sid in su_ids}
        bad = {sid: n for sid, n in counts.items() if n != 1}
    check(f"{label}: every signup in exactly one row, no ghosts",
          not ghosts and not bad, f"ghosts={ghosts} counts={bad}")


def g(*names):
    return frozenset(names)


# ---------------------------------------------------------------------------
print("\n1. A player drops from a published week")

reset()
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
row(a, b); row(c)
publish()
r = as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
check("drop succeeds", r.status_code == 200 and r.json().get("dropped"), r.text)
games, byes = state()
check("Bob plays Cat, who was sitting out, instead of a second BYE",
      games == [g("Bob", "Cat")] and byes == [], (games, byes))
check("the channel is told Bob now plays Cat",
      any("now plays" in p and "Bob" in p and "Cat" in p for p in POSTS), POSTS)
whole("published drop with someone waiting")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b)
publish()
as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
games, byes = state()
check("with nobody waiting, Bob gets a BYE", games == [] and byes == ["Bob"], (games, byes))
check("and is listed as newly without an opponent (not 'existing bye')",
      any("Bob" in p and "without an opponent" in p and "(existing bye)" not in p for p in POSTS), POSTS)
whole("published drop, nobody waiting")

reset()
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
row(a, b); row(c)
with Session(database.engine) as db:
    db.add(PairingBlock(player_a_id=PID["Bob"], player_b_id=PID["Cat"], club_id=1))
    db.commit()
publish()
as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
games, byes = state()
check("a blocked waiting player is not paired automatically",
      games == [] and byes == ["Bob", "Cat"], (games, byes))
whole("published drop, waiting player blocked")

reset()
with Session(database.engine) as db:
    cs = db.get(ClubSystem, 1)
    cs.vibe_options = [{"name": "Casual", "behaviour": "soft"},
                       {"name": "Battle March", "behaviour": "exclusive"}]
    db.add(cs)
    db.commit()
a, b = signup("Ann"), signup("Bob")
c = signup("Cat", vibe="Battle March")
row(a, b); row(c)
publish()
as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
games, byes = state()
check("a waiting player in a different exclusive format is not paired",
      games == [] and byes == ["Bob", "Cat"], (games, byes))
whole("published drop, exclusive format mismatch")
with Session(database.engine) as db:
    cs = db.get(ClubSystem, 1)
    cs.vibe_options = None
    db.add(cs)
    db.commit()

reset()
a, b = signup("Ann"), signup("Bob")
row(a); row(b)
publish()
as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
games, byes = state()
check("a player dropping off a BYE just leaves; Bob keeps his BYE",
      games == [] and byes == ["Bob"], (games, byes))
whole("published drop from a BYE")

# ---------------------------------------------------------------------------
print("\n2. A player drops from a generated but unpublished week")

reset()
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
row(a, b); row(c)
as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
games, byes = state()
check("no ghost: Bob is re-seated against Cat", games == [g("Bob", "Cat")] and byes == [], (games, byes))
check("only the plain drop post goes out (pairings are not public yet)",
      len(POSTS) == 1 and "dropped" in POSTS[0] and "now plays" not in POSTS[0], POSTS)
whole("unpublished generated drop")

print("\n3. A prearranged game loses a player before pairings exist")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b, prearranged=True)
as_player("Ann").delete(f"/signups/mine?system={TOW}&week={WEEK}")
games, byes = state()
check("the prearranged game is gone and Bob has no row yet (back in the pool)",
      games == [] and byes == [], (games, byes))
signup("Cat")
with Session(database.engine) as db:
    out = generate(db, WEEK, TOW, persist=True, club_id=1)
games, byes = state()
check("and Generate pairs him like anyone else", games == [g("Bob", "Cat")], (games, byes))
whole("prearranged drop then generate")

# ---------------------------------------------------------------------------
print("\n4. A signup arrives once the week has pairings")

reset()
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
row(a, b); row(c)
publish()
r = as_player("Dan").post("/signups", json={"system": TOW, "week": WEEK, "faction": "Skaven",
                                            "points": 2000, "eta": "18:00", "vibe": "Casual"})
check("late signup accepted", r.status_code == 200 and r.json().get("created"), r.text)
games, byes = state()
check("Dan plays Cat, who was sitting out", sorted(games, key=sorted) == sorted([g("Ann", "Bob"), g("Cat", "Dan")], key=sorted) and byes == [], (games, byes))
check("the channel hears where Dan landed", any("signed up after pairings went out" in p and "Cat" in p for p in POSTS), POSTS)
whole("late signup with someone waiting")

r = as_player("Eve").post("/signups", json={"system": TOW, "week": WEEK, "vibe": "Casual"})
games, byes = state()
check("Eve, with nobody waiting, gets a BYE rather than no row", byes == ["Eve"], (games, byes))
check("and the channel hears she is waiting", any("Eve" in p and "waiting for an opponent" in p for p in POSTS), POSTS)
whole("late signup, nobody waiting")

POSTS.clear()
r = as_player("Eve").post("/signups", json={"system": TOW, "week": WEEK, "vibe": "Competitive"})
games, byes = state()
check("editing an existing signup does not add a second row", byes == ["Eve"] and len(games) == 2, (games, byes))
check("and posts nothing about seating", not any("after pairings went out" in p for p in POSTS), POSTS)
whole("edit after late seating")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b)
as_player("Cat").post("/signups", json={"system": TOW, "week": WEEK, "vibe": "Casual"})
games, byes = state()
check("unpublished but generated: Cat is seated on a BYE", byes == ["Cat"], (games, byes))
check("with no seating post, since pairings are not public", not any("after pairings went out" in p for p in POSTS), POSTS)
whole("late signup, unpublished")

reset()
a = signup("Ann")
row(a, signup("Bob"), prearranged=True)
as_player("Cat").post("/signups", json={"system": TOW, "week": WEEK, "vibe": "Casual"})
games, byes = state()
check("before any Generate, a new signup gets no row (only prearranged games exist)",
      byes == [] and games == [g("Ann", "Bob")], (games, byes))

# ---------------------------------------------------------------------------
print("\n5. An admin force-drops a signup")

reset()
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
row(a, b); row(c)
publish()
r = ADMIN.delete(f"/admin/signups/{a}")
check("force drop succeeds", r.status_code == 200, r.text)
games, byes = state()
check("published: Bob is re-seated against Cat", games == [g("Bob", "Cat")] and byes == [], (games, byes))
check("and, because Cat's night changed, the channel is told",
      any("has been removed" in p and "now plays" in p for p in POSTS), POSTS)
whole("admin force drop, published")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b)
ADMIN.delete(f"/admin/signups/{a}")
games, byes = state()
check("unpublished generated: no ghost, Bob on a BYE", games == [] and byes == ["Bob"], (games, byes))
check("and nothing is posted", POSTS == [], POSTS)
whole("admin force drop, unpublished")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b, prearranged=True)
ADMIN.delete(f"/admin/signups/{a}")
games, byes = state()
check("before Generate: the prearranged game goes, Bob back in the pool", games == [] and byes == [], (games, byes))

# ---------------------------------------------------------------------------
print("\n6. An admin adds a signup")

reset()
# Give Hal 20 games in the past, published, so he is a Veteran.
for i in range(20):
    wk = (datetime(2026, 1, 7) + timedelta(days=7 * i)).strftime("%d/%m/%Y")
    h = signup("Hal", week=wk)
    o = signup("Gus", week=wk)
    row(h, o, week=wk)
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
row(a, b); row(c)
publish()
r = ADMIN.post("/admin/signups", json={"system": TOW, "week": WEEK, "player_id": PID["Hal"],
                                       "experience": "New", "vibe": "Casual"})
check("admin add succeeds", r.status_code == 201, r.text)
check("experience is derived (Veteran), not the form's 'New'", r.json().get("experience") == "Veteran", r.text)
games, byes = state()
check("Hal is seated against Cat, who was sitting out", g("Hal", "Cat") in games and byes == [], (games, byes))
check("and Cat's changed night is announced", any("has been added" in p and "Cat" in p for p in POSTS), POSTS)
whole("admin add, published")

# ---------------------------------------------------------------------------
print("\n7. Deleting rows in the admin grid")

reset()
a, b, c, d = signup("Ann"), signup("Bob"), signup("Cat"), signup("Dan")
g1 = row(a, b); row(c, d)
r = ADMIN.request("DELETE", "/admin/pairings", json={"system": TOW, "week": WEEK, "ids": [g1]})
check("deleting a game succeeds", r.status_code == 200 and r.json()["deleted"] == 1, r.text)
games, byes = state()
check("both players get a BYE instead of vanishing", byes == ["Ann", "Bob"] and games == [g("Cat", "Dan")], (games, byes))
whole("grid delete game")

with Session(database.engine) as db:
    ann_bye = next(p.id for p in db.exec(select(Pairing).where(Pairing.a_signup_id == a)).all())
r = ADMIN.request("DELETE", "/admin/pairings", json={"system": TOW, "week": WEEK, "ids": [ann_bye]})
check("deleting a player's only BYE is refused with a clear 409",
      r.status_code == 409 and "force drop" in r.json().get("detail", ""), r.text)
games, byes = state()
check("and nothing was deleted", byes == ["Ann", "Bob"], (games, byes))
whole("grid delete refused")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b)
ghost = row(9999)
r = ADMIN.request("DELETE", "/admin/pairings", json={"system": TOW, "week": WEEK, "ids": [ghost]})
check("a row whose signup no longer exists can still be deleted", r.status_code == 200 and r.json()["deleted"] == 1, r.text)
whole("grid delete ghost")

# ---------------------------------------------------------------------------
print("\n8. Moving players in the admin grid")


def grid_rows():
    with Session(database.engine) as db:
        return [{"id": p.id, "a_signup_id": p.a_signup_id, "b_signup_id": p.b_signup_id}
                for p in db.exec(select(Pairing).where(Pairing.week == WEEK).order_by(Pairing.id)).all()]


def save(rows):
    return ADMIN.post("/admin/pairings/save", json={"system": TOW, "week": WEEK, "rows": rows})


reset()
a, b = signup("Ann"), signup("Bob")
ra = row(a); row(b)
rows = grid_rows()
next(x for x in rows if x["id"] == ra)["b_signup_id"] = b
r = save(rows)
check("filling Ann's BYE with Bob saves", r.status_code == 200, r.text)
games, byes = state()
check("Ann plays Bob and Bob's old BYE is gone", games == [g("Ann", "Bob")] and byes == [], (games, byes))
whole("grid fill a BYE")

reset()
a, b, c = signup("Ann"), signup("Bob"), signup("Cat")
rab = row(a, b); rc = row(c)
rows = grid_rows()
next(x for x in rows if x["id"] == rc)["b_signup_id"] = a
r = save(rows)
check("putting Ann, already in a game, into Cat's BYE is refused",
      r.status_code == 409 and "already in another game" in r.json().get("detail", ""), r.text)
games, byes = state()
check("and nothing changed", games == [g("Ann", "Bob")] and byes == ["Cat"], (games, byes))
whole("grid double-booking refused")

rows = grid_rows()
next(x for x in rows if x["id"] == rab)["b_signup_id"] = c
r = save(rows)
check("replacing Bob with Cat saves", r.status_code == 200, r.text)
games, byes = state()
check("Ann plays Cat, Cat's BYE is gone, and Bob gets a BYE", games == [g("Ann", "Cat")] and byes == ["Bob"], (games, byes))
whole("grid replace a player")

reset()
a, b, c, d = signup("Ann"), signup("Bob"), signup("Cat"), signup("Dan")
r1 = row(a, b); r2 = row(c, d)
rows = grid_rows()
next(x for x in rows if x["id"] == r1)["b_signup_id"] = d
next(x for x in rows if x["id"] == r2)["b_signup_id"] = b
r = save(rows)
check("swapping two players between games in one save works", r.status_code == 200, r.text)
games, byes = state()
check("Ann-Dan and Cat-Bob, no BYEs", sorted(games, key=sorted) == sorted([g("Ann", "Dan"), g("Bob", "Cat")], key=sorted) and byes == [], (games, byes))
whole("grid swap")

rows = grid_rows()
rows[0]["b_signup_id"] = 424242
r = save(rows)
check("a player not signed up this week is refused (422)", r.status_code == 422, r.text)
rows = grid_rows()
rows[0]["b_signup_id"] = rows[0]["a_signup_id"]
r = save(rows)
check("a player against themselves is refused (422)", r.status_code == 422, r.text)
whole("grid invalid saves change nothing")

reset()
a, b = signup("Ann"), signup("Bob")
row(a, b)
row(8888)  # a row left behind by the old drop code
rows = grid_rows()
r = save(rows)
check("an untouched ghost row does not block a save", r.status_code == 200, r.text)

# ---------------------------------------------------------------------------
print("\n9. Preview shows what Generate makes")

reset()
for n in ("Ann", "Bob", "Cat", "Dan"):
    signup(n)
gen = ADMIN.post("/admin/pairings/generate", json={"system": TOW, "week": WEEK})
check("generate ok", gen.status_code == 200, gen.text)
games_after_generate, _ = state()
prev = ADMIN.post("/admin/pairings/preview", json={"system": TOW, "week": WEEK})
check("preview ok", prev.status_code == 200, prev.text)
with Session(database.engine) as db:
    preview_rows = generate(db, WEEK, TOW, persist=False, club_id=1)
preview_games = [g(x["a_name"], x["b_name"]) for x in preview_rows if x["b_signup_id"]]
check("preview after generate matches the generated games",
      sorted(preview_games, key=sorted) == sorted(games_after_generate, key=sorted),
      (preview_games, games_after_generate))

# ---------------------------------------------------------------------------
print("\n10. Who sat out last session")

reset()
# Ann: BYE 02/09, game 09/09 -> played last time.
row(signup("Ann", week="02/09/2026"), week="02/09/2026")
row(signup("Ann", week="09/09/2026"), signup("Gus", week="09/09/2026"), week="09/09/2026")
# Bob: BYE 09/09 -> sat out last time.
row(signup("Bob", week="09/09/2026"), week="09/09/2026")
# Cat: BYE 26/08 and nothing since -> her own last session was a BYE.
row(signup("Cat", week="26/08/2026"), week="26/08/2026")
# Dan: game 09/09, BYE in a LATER week than the one being paired.
row(signup("Dan", week="09/09/2026"), signup("Hal", week="09/09/2026"), week="09/09/2026")
row(signup("Dan", week="30/09/2026"), week="30/09/2026")
# Eve: a BYE row and a game in the same week -> she played.
e1 = signup("Eve", week="09/09/2026")
row(e1, week="09/09/2026")
row(e1, signup("Fay", week="09/09/2026"), week="09/09/2026")
with Session(database.engine) as db:
    ids = previous_bye_player_ids(db, TOW, WEEK, 1)
check("Bob and Cat, and nobody else", ids == {PID["Bob"], PID["Cat"]},
      {n for n, i in PID.items() if i in ids})

print("\n11. Nobody sits out twice running while an alternative exists")

reset()
# Last session: Ann v Dan, Bob v Eve, Cat on a BYE. None of D/E play now.
row(signup("Ann", week=LAST), signup("Dan", week=LAST), week=LAST)
row(signup("Bob", week=LAST), signup("Eve", week=LAST), week=LAST)
row(signup("Cat", week=LAST), week=LAST)
for n in ("Ann", "Bob", "Cat"):
    signup(n)
with Session(database.engine) as db:
    out = generate(db, WEEK, TOW, persist=False, club_id=1)
byes = [x["a_name"] for x in out if x["b_signup_id"] is None]
check("Cat, left over by the greedy match, does not sit out again", byes and byes != ["Cat"], out)
check("exactly one BYE still", len(byes) == 1, out)

with Session(database.engine) as db:
    for other in ("Ann", "Bob"):
        db.add(PairingBlock(player_a_id=min(PID["Cat"], PID[other]),
                            player_b_id=max(PID["Cat"], PID[other]), club_id=1))
    db.commit()
with Session(database.engine) as db:
    out = generate(db, WEEK, TOW, persist=False, club_id=1)
byes = [x["a_name"] for x in out if x["b_signup_id"] is None]
check("but not by pairing Cat with someone she is blocked from", byes == ["Cat"], out)

# ---------------------------------------------------------------------------
print("\n12. Experience counts games once they are official")

reset()
past_unpub = "07/01/2026"
future_pub = "30/12/2099"
future_unpub = "06/01/2100"
for wk in (past_unpub, future_pub, future_unpub):
    row(signup("Ann", week=wk), signup("Bob", week=wk), week=wk)
publish(future_pub)
with Session(database.engine) as db:
    counts = counts_for_players(db, 1, TOW, [PID["Ann"], PID["Bob"], PID["Cat"]])
check("a past week counts even if never published, a published future week counts, "
      "an unpublished future week does not",
      counts == {PID["Ann"]: 2, PID["Bob"]: 2, PID["Cat"]: 0}, counts)

# ---------------------------------------------------------------------------
print("\n13. Admin screens and call-outs read the club's own answers")

reset()
import call_outs  # noqa: E402
call_outs._post_call_out = lambda *a, **k: None
with Session(database.engine) as db:
    cs = db.get(ClubSystem, 1)
    cs.uses_standby = False
    cs.uses_points = False
    db.add(cs)
    db.commit()

cfg = ADMIN.get(f"/admin/signups?system={TOW}&week={WEEK}").json()["config"]
check("signup editor hides standby and points when the club switched them off",
      cfg["show_standby"] is False and cfg["show_points"] is False, cfg)
r = ADMIN.post("/admin/signups", json={"system": TOW, "week": WEEK, "player_id": PID["Ann"],
                                       "standby_ok": True, "points": 2500})
check("admin add ignores standby and points the club does not use",
      r.status_code == 201 and r.json()["standby_ok"] is False and r.json()["points"] is None, r.text)
sid = r.json()["id"]
r = ADMIN.patch(f"/admin/signups/{sid}", json={"standby_ok": True})
check("admin edit cannot switch standby on either", r.status_code == 200 and r.json()["standby_ok"] is False, r.text)
pc = ADMIN.get(f"/admin/pairing-config?system={TOW}").json()
check("pairing weights panel hides the points slider", pc["uses_points"] is False, pc)
r = as_player("Bob").post("/call-outs", json={"system": TOW, "game_date": "2099-01-01",
                                               "game_time": "18:00", "points": 2500})
check("a call-out carries no points when the club does not use them",
      r.status_code in (200, 201) and r.json().get("points", r.json().get("call_out", {}).get("points")) is None, r.text)

with Session(database.engine) as db:
    cs = db.get(ClubSystem, 1)
    cs.uses_standby = None
    cs.uses_points = None
    db.add(cs)
    db.commit()
cfg = ADMIN.get(f"/admin/signups?system={TOW}&week={WEEK}").json()["config"]
check("and with no override the catalogue answers again", cfg["show_standby"] is True and cfg["show_points"] is True, cfg)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
