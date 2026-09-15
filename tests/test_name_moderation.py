"""Names players type are checked for abusive words, and admins can undo one.

What these assert:
  * the usual dodges are caught (look-alike characters, spacing, stretching)
  * real names that contain a listed word are not (Hancock, Dickens, Scunthorpe)
  * the check applies to the account name and a new profile's roster name, and
    not to a club admin renaming a player, which is the fix for a false
    positive (guest +1 and tournament entry names call the same is_blocked;
    their endpoints aren't exercised here)
  * a name change is in the audit log, and a club super-admin or platform
    admin can reset an account's name

Run: PYTHONPATH=. python tests/test_name_moderation.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "moderation.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-moderation")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from models import AuditLogEntry, Club, ClubSystem, Player, SystemConfig, User  # noqa: E402
from name_moderation import is_blocked  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


print("\n1. The word check")
BLOCKED = ["fuck", "Shit Head", "sh1t", "f.u.c.k", "f u c k", "fuuuuck", "B1TCH", "Mr Wanker",
           "Nazi Steve", "motherfucker99", "c u n t", "p3n1s", "Wánker", "FuckBoy"]
ALLOWED = ["Joel Kirk", "Ian T-M", "Dick Smith", "Hancock", "Charles Dickens", "Cockburn",
           "Sussex Steve", "Scunthorpe United", "Fanny Adams", "Willy Wonka", "Assange",
           "Matthew Cummings", "Sam Butts", "Classic", "Passion", "Titus", "Therapist",
           "Snigger", "Hitchcock", "Shitake", "Nigel", "Oliver_Taylor", "Zoë", "Siobhán"]
missed = [n for n in BLOCKED if not is_blocked(n)]
wrong = [n for n in ALLOWED if is_blocked(n)]
check(f"all {len(BLOCKED)} abusive names are caught", not missed, str(missed))
check(f"none of {len(ALLOWED)} real names are blocked", not wrong, str(wrong))


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(SystemConfig(id=1, name="The Old World", slug="tow", legacy_system_name="The Old World", active=True))
    db.add(Club(id=1, name="Home", slug="home"))
    db.add(Club(id=2, name="Away", slug="away"))
    # A super-admin's authority is the club's enabled systems; a club with none
    # has nothing to administer.
    db.add(ClubSystem(club_id=1, system_id=1, enabled=True, session_day="Wednesday", session_cadence="weekly"))
    db.add(User(id=1, discord_id="d-boss", discord_name="Boss", club_id=1, is_super_admin=True))
    db.add(User(id=2, discord_id="d-pat", discord_name="Pat", club_id=1))
    db.add(User(id=3, discord_id="d-new", discord_name="Newbie", club_id=1))
    db.add(User(id=4, discord_id="d-plat", discord_name="Platform", club_id=2, is_platform_admin=True))
    db.add(Player(id=10, name="Boss", club_id=1, user_id=1))
    db.add(Player(id=20, name="Pat", club_id=1, user_id=2))
    db.commit()

H = {"origin": "https://home.calltoarms.app"}


def as_user(uid):
    c = TestClient(main.app)
    c.cookies.set("cta_session", auth._make_session_cookie(uid))
    return c


print("\n2. Where it applies")
pat = as_user(2)
r = pat.patch("/auth/account", json={"display_name": "Sh1t Lord"})
check("an abusive account name is refused", r.status_code == 422, r.text[:120])
check("with a message that doesn't repeat the word", "different name" in r.json().get("detail", ""), r.text[:120])
r = pat.patch("/auth/account", json={"display_name": "Pat the Painter"})
check("a normal one is fine", r.status_code == 200, r.text[:120])
r = as_user(3).post("/auth/create-profile", json={"name": "Wanker"}, headers=H)
check("an abusive roster name is refused at profile creation", r.status_code == 422, r.text[:120])
r = as_user(3).post("/auth/create-profile", json={"name": "Charles Dickens"}, headers=H)
check("a real name that contains a listed word is not", r.status_code == 200, r.text[:120])
boss = as_user(1)
r = boss.patch("/admin/players/20", json={"name": "Cockburn the Bold"}, headers=H)
check("a club admin's rename isn't checked, so it can fix a false positive", r.status_code == 200, r.text[:120])


print("\n3. The audit log and resetting a name")
with Session(database.engine) as db:
    logs = [(a.action, a.detail) for a in db.exec(select(AuditLogEntry).where(AuditLogEntry.action == "account.name"))]
check("the name change is logged, old and new", logs == [("account.name", "None -> 'Pat the Painter'")], str(logs))
listing = boss.get("/admin/players", headers=H).json()
check("the club's player list shows the account's chosen name",
      next(p["account_name"] for p in listing if p["id"] == 20) == "Pat the Painter", str(listing)[:200])
r = boss.post("/admin/players/20/reset-account-name", headers=H)
check("a super-admin can reset it", r.status_code == 200 and r.json()["name"] == "Pat", r.text[:120])
with Session(database.engine) as db:
    check("the account goes by its Discord name again", db.get(User, 2).display_name is None)
    check("and the reset is logged",
          db.exec(select(AuditLogEntry).where(AuditLogEntry.action == "account.name.reset")).first() is not None)
r = as_user(2).post("/admin/players/10/reset-account-name", headers=H)
check("a player who isn't a super-admin can't", r.status_code == 403, str(r.status_code))
with Session(database.engine) as db:
    db.add(Player(id=30, name="Elsewhere", club_id=2, user_id=4))
    db.commit()
r = boss.post("/admin/players/30/reset-account-name", headers=H)
check("nor can a super-admin reach another club's player", r.status_code == 404, str(r.status_code))
as_user(2).patch("/auth/account", json={"display_name": "Pat Again"})
r = as_user(4).post("/admin/platform/users/2/reset-name")
check("a platform admin can reset any account's name", r.status_code == 200 and r.json()["name"] == "Pat", r.text[:120])


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
