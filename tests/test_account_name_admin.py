"""Admins can see and undo a name an account chose.

There is no automatic filter on names (decided 2026-09-15). What stands behind
a name instead: every change is in the audit log, and a club super-admin or a
platform admin can reset an account's chosen name, which puts its Discord name
back.

What these assert:
  * a name change is logged, old and new
  * a club's player list shows each account's chosen name
  * a super-admin can reset it for a player at their club, and only there
  * a player who isn't an admin can't
  * a platform admin can reset any account's name

Run: PYTHONPATH=. python tests/test_account_name_admin.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "account_names.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-account-names")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from models import AuditLogEntry, Club, ClubSystem, Player, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


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


print("\n1. A name change is logged")
pat = as_user(2)
r = pat.patch("/auth/account", json={"display_name": "Pat the Painter"})
check("setting a name succeeds", r.status_code == 200, r.text[:120])
boss = as_user(1)


print("\n2. Resetting a name")
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
