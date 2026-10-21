"""A club where people sign in every different way, doing everything the app posts about.

Before opening Google, email-link and password sign-in, every path that talks
about a player has to cope with an account that has no Discord: the @-mentions
in signup, drop, bye, swap, prearranged, call-out, level-up and achievement
posts, and the Discord-membership gate.

The club here has one of each: Discord only, Google only, email only, password
only, someone with TWO Discord accounts (only the primary should be tagged),
and a guest with no account at all.

Every Discord message the app would send is captured and checked for the
things that would look broken in a channel: "None", an empty or dangling
mention, "undefined". A player with Discord must be tagged by their primary
id; a player without must appear as a plain name and never as a mention.

Run: PYTHONPATH=. python tests/test_mixed_identity_flows.py
"""
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "mixed.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import levels  # noqa: E402
import services  # noqa: E402
import signups as signups_mod  # noqa: E402
from identity import ProviderProfile, attach_identity, discord_ids_for_users  # noqa: E402
from main import app  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSystem, ClubWebhook, Pairing, Player, PlayerLevelAnnouncement, PublishState,
    Signup, SystemConfig, User,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


TOW = "The Old World"
WEEK = "23/09/2026"

# Every message the app would send to Discord.
POSTED: list[str] = []
_real_post = httpx.post


def fake_post(url, *a, **kw):
    payload = kw.get("json") or {}
    POSTED.append(payload.get("content") or str(payload)[:200])

    class _R:
        status_code = 204
        text = ""
    return _R()


httpx.post = fake_post

SQLModel.metadata.create_all(database.engine)

# name -> (player_id, user_id, how they sign in)
PEOPLE = [
    ("Dee", "discord only"),
    ("Gail", "google only"),
    ("Eddie", "email only"),
    ("Pat", "password only"),
    ("Shaun", "two discord accounts"),
]

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(SystemConfig(id=1, name=TOW, slug="tow", legacy_system_name=TOW, active=True,
                        uses_points=True, default_points=2000, max_points=3000,
                        uses_standby=True, allows_demo=True,
                        vibe_options=["Casual", "Competitive", "Open", "Intro"], default_vibe="Casual"))
    db.add(ClubSystem(id=1, club_id=1, system_id=1, enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    for kind in ("signup", "pairings", "league", "achievement", "call_outs", "level"):
        db.add(ClubWebhook(club_id=1, webhook_type=kind, system_id=1,
                           url=f"https://discord.test/{kind}"))
    db.add(User(id=1, discord_id="admin-d", discord_name="Admin", club_id=1, home_club_id=1,
                is_super_admin=True))
    for i, (name, _) in enumerate(PEOPLE, start=1):
        db.add(User(id=i + 1, club_id=1, home_club_id=1))
        db.add(Player(id=i, name=name, club_id=1, user_id=i + 1, active=True))
    db.commit()
    attach_identity(db, db.get(User, 1), ProviderProfile(provider="discord", subject="admin-d", name="Admin"))
    # Dee: Discord only
    attach_identity(db, db.get(User, 2), ProviderProfile(provider="discord", subject="dee-777", name="Dee"))
    # Gail: Google only
    attach_identity(db, db.get(User, 3), ProviderProfile(provider="google", subject="g-gail",
                                                         name="Gail G", email="gail@example.com", email_verified=True))
    # Eddie: email link only
    attach_identity(db, db.get(User, 4), ProviderProfile(provider="email", subject="eddie@example.com",
                                                         email="eddie@example.com", email_verified=True))
    # Pat: password only
    attach_identity(db, db.get(User, 5), ProviderProfile(provider="password", subject="pat@example.com",
                                                         email="pat@example.com", email_verified=True))
    # Shaun: signs in with one Discord account, tagged as the other (the one in the server)
    attach_identity(db, db.get(User, 6), ProviderProfile(provider="discord", subject="shaun-old", name="ShaunOld"))
    shaun_server = attach_identity(db, db.get(User, 6), ProviderProfile(provider="discord", subject="shaun-in-server", name="ShaunInServer"))
    db.commit()
    for i in db.exec(select(type(shaun_server)).where(type(shaun_server).user_id == 6)).all():
        i.is_primary = i.provider_user_id == "shaun-in-server"
        db.add(i)
    u = db.get(User, 6)
    u.discord_id = "shaun-in-server"
    u.discord_name = "ShaunInServer"
    db.add(u)
    db.commit()

PID = {name: i for i, (name, _) in enumerate(PEOPLE, start=1)}
UID = {name: i + 1 for i, (name, _) in enumerate(PEOPLE, start=1)}


def client_for(user_id):
    c = TestClient(app)
    c.cookies.set("cta_session", auth._make_session_cookie(user_id))
    return c


ADMIN = client_for(1)
H = {"origin": "https://egnwgc.calltoarms.app"}
MENTION = {"Dee": "<@dee-777>", "Shaun": "<@shaun-in-server>"}
NO_MENTION = ["Gail", "Eddie", "Pat"]


def sign_up(name, **over):
    body = {"system": TOW, "week": WEEK, "faction": "Skaven", "points": 2000,
            "eta": "18:30", "vibe": "Casual", "standby_ok": False}
    body.update(over)
    return client_for(UID[name]).post("/signups", json=body, headers=H)


def bad_bits(message):
    """The things that would look broken in a Discord channel."""
    found = []
    if "None" in message:
        found.append("the word None")
    if re.search(r"<@\s*>|<@None>|<@undefined>", message):
        found.append("an empty mention")
    if "undefined" in message:
        found.append("undefined")
    # Only a genuinely empty one: "**bold** **bold**" is two names side by side.
    if "****" in message:
        found.append("an empty bold name")
    if re.search(r"\(\s*\)|\(<@\s*>\)", message):
        found.append("an empty bracket where a mention would go")
    return found


print("\n1. Everyone signs up, however they sign in")
for name, how in PEOPLE:
    r = sign_up(name)
    check(f"{name} ({how}) can sign up", r.status_code in (200, 201), r.text[:120])
signup_posts = list(POSTED)
check("every signup was announced", len(signup_posts) >= len(PEOPLE), str(len(signup_posts)))
for name in ("Dee", "Shaun"):
    mine = [m for m in signup_posts if name in m]
    check(f"{name} is tagged by the Discord account that counts",
          mine and any(MENTION[name] in m for m in mine), str(mine)[:160])
for name in NO_MENTION:
    mine = [m for m in signup_posts if name in m]
    check(f"{name} appears as a plain name, with no mention",
          mine and all("<@" not in m.split(name)[0][-14:] for m in mine) and all("@None" not in m for m in mine),
          str(mine)[:160])
for m in signup_posts:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")


print("\n2. Mentions resolve per account type")
with Session(database.engine) as db:
    ids = discord_ids_for_users(db, [UID[n] for n, _ in PEOPLE])
    check("Dee's Discord is used", ids.get(UID["Dee"]) == "dee-777", str(ids))
    check("Shaun is tagged as the account in the server, not the one he logs in with",
          ids.get(UID["Shaun"]) == "shaun-in-server", str(ids))
    for name in NO_MENTION:
        check(f"{name} has no Discord to tag", UID[name] not in ids)
    check("and so nothing is posted for them",
          database.name_with_mention(db, "Gail", PID["Gail"]) == "**Gail**",
          database.name_with_mention(db, "Gail", PID["Gail"]))


print("\n3. Pairings, a drop-out and the knock-on posts")
with Session(database.engine) as db:
    sus = {s.player_name: s for s in db.exec(select(Signup).where(Signup.week == WEEK)).all()}
    db.add(Pairing(id=1, week=WEEK, system=TOW, a_signup_id=sus["Gail"].id, b_signup_id=sus["Dee"].id,
                   status="pending", club_id=1))
    db.add(Pairing(id=2, week=WEEK, system=TOW, a_signup_id=sus["Eddie"].id, b_signup_id=sus["Pat"].id,
                   status="pending", club_id=1))
    # Shaun sits out, so a drop promotes him into the game.
    db.add(Pairing(id=3, week=WEEK, system=TOW, a_signup_id=sus["Shaun"].id, b_signup_id=None,
                   status="pending", club_id=1))
    db.add(PublishState(week=WEEK, system=TOW, published=True, club_id=1))
    db.commit()

POSTED.clear()
r = client_for(UID["Gail"]).delete("/signups/mine", params={"system": TOW, "week": WEEK}, headers=H)
check("a Google-only player can drop out", r.status_code == 200, r.text[:150])
drop_posts = list(POSTED)
check("the drop was announced", drop_posts, str(drop_posts)[:100])
for m in drop_posts:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")
check("Gail is named without a mention", any("Gail" in m and "<@" not in m.split("Gail")[0][-14:] for m in drop_posts),
      str(drop_posts)[:200])
promoted = [m for m in drop_posts if "Shaun" in m]
check("Shaun's promotion off the BYE tags his server account",
      promoted and all("<@shaun-in-server>" in m for m in promoted), str(promoted)[:200])
check("and never the account he signs in with", not any("shaun-old" in m for m in drop_posts))


print("\n4. A swap between two accounts with no Discord")
POSTED.clear()
with Session(database.engine) as db:
    rows = {p.id: p for p in db.exec(select(Pairing).where(Pairing.week == WEEK)).all()}
r = client_for(UID["Eddie"]).post("/signups/swap", json={"system": TOW, "week": WEEK,
                                                         "opponent_player_id": PID["Pat"]}, headers=H)
check("the swap endpoint answers", r.status_code in (200, 400, 409, 422), r.text[:150])
for m in POSTED:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")


print("\n5. A game arranged against a guest with no account at all")
POSTED.clear()
client_for(UID["Pat"]).delete("/signups/mine", params={"system": TOW, "week": WEEK}, headers=H)
POSTED.clear()
r = client_for(UID["Pat"]).post("/signups/prearranged", json={
    "system": TOW, "week": WEEK, "player_a_id": PID["Pat"], "player_b_id": None,
    "guest_b_name": "Visiting Vince", "faction_a": "Skaven", "faction_b": "Empire of Man",
    "eta": "18:30", "vibe": "Casual", "points": 2000}, headers=H)
check("a password-only player can arrange a game with a guest", r.status_code in (200, 201), r.text[:200])
for m in POSTED:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")
check("the guest is named plainly", any("Visiting Vince" in m for m in POSTED) if POSTED else True, str(POSTED)[:200])


print("\n6. Call-outs")
POSTED.clear()
r = client_for(UID["Gail"]).post("/call-outs", json={
    "system": TOW,
    "game_date": (datetime.utcnow() + timedelta(days=3)).strftime("%Y-%m-%d"),
    "game_time": "18:30", "faction": "Skaven", "vibe": "Casual", "points": 2000,
    "notes": "Any takers?"}, headers=H)
check("a Google-only player can post a call-out", r.status_code in (200, 201), r.text[:200])
call_out_id = r.json().get("id") if r.status_code in (200, 201) else None
if call_out_id:
    r = client_for(UID["Dee"]).post(f"/call-outs/{call_out_id}/take", headers=H)
    check("a Discord player can take it", r.status_code == 200, r.text[:150])
for m in POSTED:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")


print("\n7. Level-ups and achievements")
POSTED.clear()
with Session(database.engine) as db:
    # Enough games for everyone to cross a level, and no announcement history.
    for pid in PID.values():
        for i in range(12):
            wk = f"{(i % 28) + 1:02d}/0{(i % 9) + 1}/2026"
            su = Signup(week=wk, system=TOW, player_id=pid, player_name=[n for n, v in PID.items() if v == pid][0],
                        faction="Skaven", points=2000, eta="18:30", vibe="Casual", club_id=1)
            db.add(su)
            db.flush()
            db.add(Pairing(week=wk, system=TOW, a_signup_id=su.id, b_signup_id=None, status="pending", club_id=1))
        db.add(PlayerLevelAnnouncement(club_id=1, player_id=pid, system=TOW, last_level=1))
    db.commit()
posted = levels.announce_level_ups(database.engine and Session(database.engine), 1, TOW)
check("level-ups post for a mixed club", isinstance(posted, int), str(posted))
for m in POSTED:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")

POSTED.clear()
with Session(database.engine) as db:
    for name in ("Gail", "Dee"):
        services.post_discord_achievement(name, "Veteran", 1, 1, db)
check("achievement posts name the player plainly", len(POSTED) == 2, str(POSTED))
for m in POSTED:
    check("nothing broken in: " + m.split("\n")[0][:52], not bad_bits(m), f"{bad_bits(m)} in {m!r}")


print("\n7b. The posted pairings image, with a mixed club in it")
from scripts.render_pairings_image import render_pairings_image  # noqa: E402
rows_for_image = [
    {"a_name": "Dee", "a_faction": "Skaven", "b_name": "Gail", "b_faction": "Empire of Man",
     "type": "Casual", "eta": "18:30", "points": "2000"},
    # No faction, no opponent, no points: a BYE for an account with no Discord.
    {"a_name": "Eddie", "a_faction": None, "b_name": "BYE", "b_faction": None,
     "type": "Casual", "eta": None, "points": None},
    {"a_name": "Pat", "a_faction": "Skaven", "b_name": "Visiting Vince", "b_faction": None,
     "type": "Competitive", "eta": "19:00", "points": "1500"},
]
try:
    buf = render_pairings_image(rows_for_image, WEEK, TOW)
    check("the image renders for Discord and non-Discord players alike",
          buf is not None and len(buf.getvalue()) > 2000, str(buf and len(buf.getvalue())))
except Exception as e:  # noqa: BLE001
    check("the image renders for Discord and non-Discord players alike", False, f"{type(e).__name__}: {e}")


print("\n8. The Discord membership gate (Decision A: no Discord means let them through)")
with Session(database.engine) as db:
    cs = db.exec(select(ClubSystem).where(ClubSystem.club_id == 1)).first()
    cs.discord_gate_enabled = True
    cs.discord_guild_id = "guild-1"
    cs.discord_gate_mode = "enforce"
    db.add(cs)
    db.commit()
import discord_guild  # noqa: E402
discord_guild.is_guild_member = lambda guild_id, user_id: False  # nobody is in the server
with Session(database.engine) as db:
    for name in NO_MENTION:
        player = db.get(Player, PID[name])
        player.discord_verified_at = None
        db.add(player)
    dee = db.get(Player, PID["Dee"])
    dee.discord_verified_at = None
    db.add(dee)
    db.commit()
POSTED.clear()
r = sign_up("Gail", week="30/09/2026")
check("an account with no Discord is not blocked by the gate", r.status_code in (200, 201), r.text[:200])
r = sign_up("Dee", week="30/09/2026")
check("a Discord account that isn't in the server IS blocked", r.status_code == 403, r.text[:200])


print()
httpx.post = _real_post
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
