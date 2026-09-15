"""Account overhaul Slab 6: adding, choosing and removing sign-in methods, and
merging two accounts once someone has proven they hold both.

Providers are stubbed (auth._discord_profile / _google_profile).

What these assert:
  * a second Discord account can be added, and chosen as the one posts tag,
    which is the fix for KNOWN_ISSUES.md #1
  * a sign-in method can be removed, but never the last one; removing one ends
    other sessions and keeps this browser signed in; removing an account's only
    Discord keeps the name it went by
  * linking a method that belongs to another account offers a merge rather than
    refusing; the preview shows what would move; confirming merges; the offer is
    bound to the account and session that proved both, and expires
  * a merge the Slab 1 rules refuse is refused here too, writing nothing

Run: PYTHONPATH=. python tests/test_link_unlink.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_DB = pathlib.Path(tempfile.mkdtemp()) / "link.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-link")
os.environ.setdefault("DISCORD_CLIENT_ID", "discord-client")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from identity import ProviderProfile, attach_identity, discord_ids_for_users  # noqa: E402
from models import AuditLogEntry, Club, Player, User, UserIdentity  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


auth.DISCORD_CLIENT_ID = "discord-client"
auth.GOOGLE_CLIENT_ID, auth.GOOGLE_CLIENT_SECRET, auth.GOOGLE_SIGNIN = "gid", "gsecret", "link"

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="Home", slug="home"))
    db.add(Club(id=2, name="Away", slug="away"))
    craig = User(id=1, discord_id="d-craig", discord_name="CraigLogin", club_id=1, home_club_id=1)
    old = User(id=2, discord_id="d-craig-old", discord_name="CraigOld", club_id=1, home_club_id=1)
    solo = User(id=3, discord_id="d-solo", discord_name="Solo", club_id=1, home_club_id=1)
    clash = User(id=4, discord_id="d-clash", discord_name="Clash", club_id=1, home_club_id=1)
    for u in (craig, old, solo, clash):
        db.add(u)
    db.flush()
    attach_identity(db, craig, ProviderProfile(provider="discord", subject="d-craig", name="CraigLogin"))
    attach_identity(db, old, ProviderProfile(provider="discord", subject="d-craig-old", name="CraigOld"))
    attach_identity(db, solo, ProviderProfile(provider="discord", subject="d-solo", name="Solo"))
    attach_identity(db, clash, ProviderProfile(provider="discord", subject="d-clash", name="Clash"))
    db.add(Player(id=10, name="Craig", club_id=1, user_id=1))
    db.add(Player(id=20, name="Craig (old)", club_id=2, user_id=2))
    db.add(Player(id=40, name="Clash", club_id=1, user_id=4))
    db.commit()

PROFILES = {}


async def fake_profile(code):
    return PROFILES[code]


auth._discord_profile = fake_profile
auth._google_profile = fake_profile

H = {"origin": "https://home.calltoarms.app"}


def client_for(user_id, version=0):
    c = TestClient(main.app, follow_redirects=False, headers=H)
    c.cookies.set("cta_session", auth._make_session_cookie(user_id, version))
    return c


def version_of(user_id):
    with Session(database.engine) as db:
        return db.get(User, user_id).session_version or 0


def link(provider, user_id, code, client=None):
    c = client or client_for(user_id, version_of(user_id))
    with Session(database.engine) as db:
        c.cookies.set("cta_oauth_link", auth._link_cookie_value(db.get(User, user_id)))
    c.cookies.set("cta_oauth_state", "st")
    c.cookies.set("cta_oauth_return_to", "https://home.calltoarms.app")
    r = c.get(f"/auth/{provider}/callback", params={"state": "st", "code": code})
    return c, r


def idents(user_id):
    with Session(database.engine) as db:
        return {i.provider_user_id: i for i in db.exec(select(UserIdentity).where(UserIdentity.user_id == user_id))}


print("\n1. A second Discord account, chosen for posts (KNOWN_ISSUES.md #1)")
check("/account offers adding Discord to a Google-only account's list of options",
      "discord" in auth.linkable_providers())
r = client_for(1).get("/auth/discord/link", headers={"referer": "https://home.calltoarms.app/account"})
check("adding Discord goes to Discord with the link remembered",
      "discord.com" in r.headers.get("location", "") and "cta_oauth_link=" in " ".join(r.headers.get_list("set-cookie")))
PROFILES["craig-server"] = ProviderProfile(provider="discord", subject="d-craig-server", name="CraigInServer")
_, r = link("discord", 1, "craig-server")
check("the Discord account in the server is added", r.headers.get("location", "").endswith("/account?linked=discord"),
      r.headers.get("location"))
check("as a second Discord identity, not the primary", idents(1)["d-craig-server"].is_primary is False)
check("posts still tag the one he signs in with", discord_ids_for_users(Session(database.engine), [1]) == {1: "d-craig"})
server_id = idents(1)["d-craig-server"].id
r = client_for(1, version_of(1)).post(f"/auth/identities/{server_id}/primary")
check("choosing the server account succeeds", r.status_code == 200, r.text[:120])
with Session(database.engine) as db:
    check("now posts tag the account that's in the server", discord_ids_for_users(db, [1]) == {1: "d-craig-server"})
    u = db.get(User, 1)
    check("and the mirror follows it", (u.discord_id, u.discord_name) == ("d-craig-server", "CraigInServer"))
    check("both still sign in", {i.user_id for i in db.exec(select(UserIdentity).where(
        UserIdentity.provider_user_id.in_(["d-craig", "d-craig-server"])))} == {1})
r = client_for(3, version_of(3)).post(f"/auth/identities/{server_id}/primary")
check("nobody else can touch someone's sign-in methods", r.status_code == 404)


print("\n2. Removing a sign-in method")
solo_only = next(iter(idents(3).values())).id
r = client_for(3, version_of(3)).delete(f"/auth/identities/{solo_only}")
check("an account's only way in can't be removed", r.status_code == 409, r.text[:120])
before = version_of(1)
c = client_for(1, before)
r = c.delete(f"/auth/identities/{server_id}")
check("removing the primary Discord succeeds", r.status_code == 200, r.text[:120])
fresh = next((h.split(";")[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie") if h.startswith("cta_session=")), None)
check("other sessions are ended", version_of(1) == before + 1)
check("this browser gets a fresh cookie and stays signed in",
      fresh and TestClient(main.app, cookies={"cta_session": fresh}).get("/auth/me").json().get("authenticated") is True)
check("the old cookie no longer works", client_for(1, before).get("/auth/me").json().get("authenticated") is False)
with Session(database.engine) as db:
    check("his other Discord takes over as the one posts tag", discord_ids_for_users(db, [1]) == {1: "d-craig"})
    check("the removal is in the audit log",
          db.exec(select(AuditLogEntry).where(AuditLogEntry.action == "identity.unlink")).first() is not None)

PROFILES["solo-google"] = ProviderProfile(provider="google", subject="g-solo", name="Solo G", email="solo@example.com", email_verified=True)
link("google", 3, "solo-google")
r = client_for(3, version_of(3)).delete(f"/auth/identities/{solo_only}")
with Session(database.engine) as db:
    u = db.get(User, 3)
    check("with Google added, Solo can remove his only Discord", r.status_code == 200, r.text[:100])
    check("his account keeps the name it went by", (u.display_name, u.discord_name, u.discord_id) == ("Solo", None, None),
          str((u.display_name, u.discord_name, u.discord_id)))
    check("and nothing tags a Discord account for him any more", discord_ids_for_users(db, [3]) == {})


print("\n3. Linking something that already has its own account offers a merge")
PROFILES["craig-old"] = ProviderProfile(provider="discord", subject="d-craig-old", name="CraigOld")
c, r = link("discord", 1, "craig-old")
check("the offer, not a refusal", r.headers.get("location", "").endswith("/account?merge=pending&provider=discord"),
      r.headers.get("location"))
merge_cookie = next(h.split(";")[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie") if h.startswith("cta_merge="))
c = client_for(1, version_of(1))
c.cookies.set("cta_merge", merge_cookie)
preview = c.get("/auth/merge")
body = preview.json() if preview.status_code == 200 else {}
check("the preview names the other account and what it holds",
      preview.status_code == 200 and body["other"]["name"] == "CraigOld"
      and body["other"]["players"] == [{"name": "Craig (old)", "club": "Away"}] and body["problems"] == [],
      preview.text[:200])
other = client_for(4, version_of(4))
other.cookies.set("cta_merge", merge_cookie)
check("another account can't use the offer", other.get("/auth/merge").status_code == 404)
r = c.post("/auth/merge/confirm")
check("confirming merges", r.status_code == 200, r.text[:150])
with Session(database.engine) as db:
    check("the other account is gone", db.get(User, 2) is None)
    check("its player and its Discord login are Craig's now",
          db.get(Player, 20).user_id == 1 and db.exec(select(UserIdentity).where(
              UserIdentity.provider_user_id == "d-craig-old")).first().user_id == 1)
    check("the merge is in the audit log",
          db.exec(select(AuditLogEntry).where(AuditLogEntry.action == "account.merge")).first() is not None)

PROFILES["clash"] = ProviderProfile(provider="discord", subject="d-clash", name="Clash")
_, r = link("discord", 1, "clash")
merge_cookie = next(h.split(";")[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie") if h.startswith("cta_merge="))
c = client_for(1, version_of(1))
c.cookies.set("cta_merge", merge_cookie)
check("a merge the rules forbid shows why in the preview",
      any("merge those players first" in p for p in c.get("/auth/merge").json()["problems"]))
r = c.post("/auth/merge/confirm")
with Session(database.engine) as db:
    check("and confirming it is refused, writing nothing",
          r.status_code == 409 and db.get(User, 4) is not None and db.get(Player, 40).user_id == 4, r.text[:150])

with Session(database.engine) as db:
    u = db.get(User, 1)
    stale = f"merge:1:4:{u.session_version}:{int((datetime.utcnow() - timedelta(minutes=1)).timestamp())}"
stale_cookie = f"{stale}.{auth._sign(stale)}"
c.cookies.set("cta_merge", stale_cookie)
check("an expired offer does nothing", c.get("/auth/merge").status_code == 404)
tampered = merge_cookie.replace("merge:1:4", "merge:1:3")
c.cookies.set("cta_merge", tampered)
check("a tampered offer does nothing", c.get("/auth/merge").status_code == 404)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
