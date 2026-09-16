"""Account overhaul Slab 0: identities are separate from accounts.

Nothing here is visible to a player yet. What it guards is the foundation the
rest of the overhaul stands on (ACCOUNT_OVERHAUL.md §7):

  * sessions can be ended, and the deploy that introduces that logs nobody out
  * sign-in is one shared path, so a second provider is a small module
  * user_identities decides which Discord account a user has, for sign-in,
    @-mentions and the guild gate alike, so they can never disagree
  * a sign-in never writes the name the user owns (the §2 trap)
  * a club can be requested and provisioned from a non-Discord identity
  * /auth/me says only what it means to

Run: PYTHONPATH=. python tests/test_identity_decoupling.py
"""
import base64
import json
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "identity.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-identity")
os.environ.setdefault("EMAIL_FROM", "notifications@calltoarms.app")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from identity import (  # noqa: E402
    ProviderProfile, attach_identity, discord_ids_for_users,
)
from models import Club, ClubRequest, Player, SystemConfig, User, UserIdentity  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(SystemConfig(id=1, name="The Old World", slug="tow",
                        legacy_system_name="The Old World", active=True))
    db.add(Club(id=1, name="Home Club", slug="home"))
    # Accounts as the backfill-less old code left them: a Discord ID on the
    # users row and no identity row.
    db.add(User(id=1, discord_id="d-admin", discord_name="Joel", club_id=1,
                is_platform_admin=True))
    db.add(User(id=2, discord_id="d-ian", discord_name="Iantm", club_id=1,
                display_name="Ian T-M"))
    db.add(Player(id=10, name="Ian T-M", club_id=1, user_id=2))
    db.commit()

import club_emails  # noqa: E402

club_emails.send_email = lambda *a, **k: "fake-message-id"

client = TestClient(main.app)


def cookie_from(response, name):
    for header in response.headers.getlist("set-cookie"):
        if header.startswith(f"{name}="):
            return header.split(";", 1)[0].split("=", 1)[1].strip('"')
    return None


print("\n1. Existing sessions survive the deploy")
legacy = f"2.{auth._sign('2')}"
check("version 0 is the original cookie, byte for byte", auth._make_session_cookie(2) == legacy)
client.cookies.set("cta_session", legacy)
me = client.get("/auth/me").json()
check("a pre-Slab-0 cookie still signs in", me.get("authenticated") is True, str(me)[:120])
client.cookies.set("cta_session", "2." + "0" * 64)
check("a forged signature does not", client.get("/auth/me").json().get("authenticated") is False)
client.cookies.set("cta_session", f"1:0.{auth._sign('2:0')}")
check("a signature for another body does not", client.get("/auth/me").json().get("authenticated") is False)


print("\n1b. The 30 days roll forward while someone keeps using the app")
import datetime as _dt
fresh_now = auth._make_session_cookie(2, 0, int(_dt.datetime.utcnow().timestamp()))
client.cookies.set("cta_session", fresh_now)
r = client.get("/auth/me")
check("a stamped, recent cookie signs in", r.json().get("authenticated") is True)
check("and is left alone", not any(h.startswith("cta_session=") for h in r.headers.get_list("set-cookie")),
      str(r.headers.get_list("set-cookie")))
old_stamp = int((_dt.datetime.utcnow() - _dt.timedelta(days=8)).timestamp())
client.cookies.set("cta_session", auth._make_session_cookie(2, 0, old_stamp))
r = client.get("/auth/me")
issued = next((h for h in r.headers.get_list("set-cookie") if h.startswith("cta_session=")), None)
check("a cookie over a week old is replaced, so the window moves", issued is not None, str(r.headers.get_list("set-cookie")))
check("with another 30 days on it", issued and "Max-Age=2592000" in issued, issued or "")
client.cookies.set("cta_session", legacy)
r = client.get("/auth/me")
check("a cookie from before stamping existed still signs in and gets a date",
      r.json().get("authenticated") is True
      and any(h.startswith("cta_session=") for h in r.headers.get_list("set-cookie")))
check("a cookie with junk where the date goes is refused",
      client.get("/auth/me", headers={"cookie": f"cta_session=2:0:x.{auth._sign('2:0:x')}"}).json().get("authenticated") is False)
check("and so is one with an extra field",
      client.get("/auth/me", headers={"cookie": f"cta_session=2:0:1:1.{auth._sign('2:0:1:1')}"}).json().get("authenticated") is False)


print("\n2. /auth/me says what it means to, and no more")
client.cookies.set("cta_session", legacy)
user = client.get("/auth/me").json()["user"]
check("greets by the name the user chose", user.get("name") == "Ian T-M", str(user))
check("still carries the Discord handle for the UI that shows it", user.get("discord_name") == "Iantm")
for hidden in ("session_version", "discord_id", "created_at", "last_login_at"):
    check(f"does not expose {hidden}", hidden not in user)


print("\n3. A returning Discord sign-in heals the identity and leaves the user's name alone")
resp = auth._finish_sign_in(
    Session(database.engine),
    ProviderProfile(provider="discord", subject="d-ian", name="Ian (new handle)",
                    avatar_url="https://cdn.discordapp.com/avatars/d-ian/x.png"),
    "https://home.calltoarms.app", "/signup?system=The+Old+World",
)
check("lands where they were going",
      resp.headers["location"] == "https://home.calltoarms.app/signup?system=The+Old+World",
      resp.headers.get("location"))
with Session(database.engine) as db:
    ident = db.exec(select(UserIdentity).where(UserIdentity.user_id == 2)).all()
    u = db.get(User, 2)
    check("the missing identity row was created", len(ident) == 1 and ident[0].provider_user_id == "d-ian")
    check("the Discord handle follows Discord", u.discord_name == "Ian (new handle)")
    check("display_name is untouched", u.display_name == "Ian T-M", u.display_name)
    check("the identity carries what the provider said", ident and ident[0].name == "Ian (new handle)")
session_cookie = cookie_from(resp, "cta_session")
check("a session cookie was issued", session_cookie is not None)
check("the OAuth cookies are cleared", cookie_from(resp, "cta_oauth_state") in ("", None))


print("\n4. Signing out everywhere ends every session, including old ones")
client.cookies.set("cta_session", legacy)
r = client.post("/auth/logout-everywhere")
check("accepted", r.status_code == 200, r.text[:100])
with Session(database.engine) as db:
    check("the account's session version moved", db.get(User, 2).session_version == 1)
client.cookies.set("cta_session", legacy)
check("the old cookie no longer signs in", client.get("/auth/me").json().get("authenticated") is False)
client.cookies.set("cta_session", session_cookie)
check("nor does the one issued a moment ago", client.get("/auth/me").json().get("authenticated") is False)
resp = auth._finish_sign_in(Session(database.engine),
                            ProviderProfile(provider="discord", subject="d-ian", name="Ian"),
                            None, None)
fresh = cookie_from(resp, "cta_session")
check("signing in again issues a cookie at the new version", fresh and fresh.startswith("2:1:"), fresh)
client.cookies.set("cta_session", fresh)
check("which works", client.get("/auth/me").json().get("authenticated") is True)


print("\n5. A brand-new person is deferred, then becomes one account with one identity")
resp = auth._finish_sign_in(Session(database.engine),
                            ProviderProfile(provider="discord", subject="d-new", name="Newbie"),
                            "https://home.calltoarms.app", "/signup")
check("sent to the club picker with next kept",
      resp.headers["location"] == "https://home.calltoarms.app/join?next=%2Fsignup",
      resp.headers.get("location"))
pending = cookie_from(resp, "cta_pending_signup")
check("no session yet", cookie_from(resp, "cta_session") is None)
client.cookies.clear()
client.cookies.set("cta_pending_signup", pending)
r = client.post("/auth/complete-signup", json={"club_id": 1})
check("complete-signup succeeds", r.status_code == 200, r.text[:150])
check("and answers with the explicit user shape", "session_version" not in r.json().get("user", {}))
r2 = client.post("/auth/complete-signup", json={"club_id": 1})
with Session(database.engine) as db:
    users = db.exec(select(User).where(User.discord_id == "d-new")).all()
    idents = db.exec(select(UserIdentity).where(UserIdentity.provider_user_id == "d-new")).all()
    check("a double submit still makes exactly one account", len(users) == 1, str(len(users)))
    check("with exactly one identity row", len(idents) == 1 and idents[0].user_id == users[0].id)


print("\n6. The pending cookie is provider-neutral, and still reads the old shape")
old_body = base64.urlsafe_b64encode(json.dumps(
    {"discord_id": "d-old", "discord_name": "Old", "avatar_url": None}).encode()).decode()
old = auth._verify_pending_signup_cookie(f"{old_body}.{auth._sign(old_body)}")
check("a cookie issued before the deploy is understood",
      old is not None and (old.provider, old.subject, old.name) == ("discord", "d-old", "Old"))
g = auth._verify_pending_signup_cookie(auth._make_pending_profile_cookie(
    ProviderProfile(provider="google", subject="g-123", name="Gail", email="gail@x.test",
                    email_verified=True)))
check("a non-Discord identity round-trips",
      g is not None and (g.provider, g.subject, g.email, g.email_verified) == ("google", "g-123", "gail@x.test", True))
check("tampering is refused", auth._verify_pending_signup_cookie(f"{old_body}.bad") is None)


print("\n7. One authority for 'which Discord account': mentions and the gate")
with Session(database.engine) as db:
    check("the healed account mentions its Discord ID",
          database.discord_mentions_for_player_ids(db, [10]) == {10: "<@d-ian>"},
          str(database.discord_mentions_for_player_ids(db, [10])))
    check("a not-yet-backfilled account falls back to the mirror",
          discord_ids_for_users(db, [1]) == {1: "d-admin"})
    # Stale mirror: the identity row says one thing, users.discord_id another.
    u = db.get(User, 2)
    u.discord_id = "d-stale"
    db.add(u)
    db.commit()
    check("the identity row wins over a stale mirror", discord_ids_for_users(db, [2]) == {2: "d-ian"})
    # Unlinking Discord (Slab 6 will do this): identity gone, mirror cleared.
    for i in db.exec(select(UserIdentity).where(UserIdentity.user_id == 2)).all():
        db.delete(i)
    u.discord_id = None
    db.add(u)
    db.commit()
    check("an unlinked account is never mentioned",
          database.discord_mentions_for_player_ids(db, [10]) == {})
    import identity
    check("and the gate sees no Discord account (so lets them through, Decision A)",
          identity.discord_id_for_user(db, 2) is None)
with Session(database.engine) as db:
    check("an account with only the mirror is found by it (and healed)",
          identity.find_user_for_profile(
              db, ProviderProfile(provider="discord", subject="d-admin", name="x")).id == 1)
    db.add(User(id=50, discord_id="d-stale-3", discord_name="Stale", club_id=1))
    db.flush()
    db.add(UserIdentity(user_id=50, provider="discord", provider_user_id="d-real-3", is_primary=True))
    db.commit()
    check("a sign-in matching only a stale mirror doesn't take over an account",
          identity.find_user_for_profile(
              db, ProviderProfile(provider="discord", subject="d-stale-3", name="x")) is None)
    # A second Discord account on one person (Shaun): both sign in, one is
    # the Discord account that counts.
    admin = db.get(User, 1)
    attach_identity(db, admin, ProviderProfile(provider="discord", subject="d-admin-alt", name="Alt"))
    db.commit()
    check("a second Discord identity signs into the same account",
          identity.find_user_for_profile(
              db, ProviderProfile(provider="discord", subject="d-admin-alt", name="Alt")).id == 1)
    check("but the first stays primary, for mentions and the gate",
          discord_ids_for_users(db, [1]) == {1: "d-admin"})
    check("and the mirror still names the primary", db.get(User, 1).discord_id == "d-admin")
    resp = auth._finish_sign_in(db, ProviderProfile(provider="discord", subject="d-admin-alt",
                                                    name="Alt handle"), None, None)
    check("signing in with the secondary doesn't rename the account's Discord handle",
          db.get(User, 1).discord_name == "Joel", db.get(User, 1).discord_name)
    other = db.get(User, 1)
    try:
        attach_identity(db, other, ProviderProfile(provider="discord", subject="d-new", name="x"))
        moved = True
    except ValueError:
        moved = False
    db.rollback()
    check("an identity is never moved between accounts by attaching it", moved is False)


print("\n8. A club can be requested and provisioned from a non-Discord identity")
client.cookies.clear()
client.cookies.set("cta_pending_signup", auth._make_pending_profile_cookie(
    ProviderProfile(provider="google", subject="g-org", name="Gwen")))
check("the form knows who is asking", client.get("/club-requests/identity").json() ==
      {"signed_in": True, "name": "Gwen", "discord_name": None},
      str(client.get("/club-requests/identity").json()))
REQUEST = {
    "requester_name": "Gwen", "requester_email": "gwen@x.test", "club_name": "Google Grove",
    "club_location": "Leeds", "region": "Yorkshire & the Humber", "preferred_slug": "grove",
    "systems": ["The Old World"], "club_night_day": "Tuesday", "club_night_time": "18:00",
    "player_count": 10, "requester_role": "Club organiser",
    "evidence_url": "https://example.test/grove", "notes": "",
}
r = client.post("/club-requests", json=REQUEST)
check("accepted", r.status_code == 201, r.text[:150])
check("a second pending request from the same identity is refused",
      client.post("/club-requests", json={**REQUEST, "club_name": "Again"}).status_code == 409)
with Session(database.engine) as db:
    req = db.exec(select(ClubRequest).where(ClubRequest.club_name == "Google Grove")).first()
    check("the identity is recorded provider-neutrally",
          req and (req.identity_provider, req.identity_subject) == ("google", "g-org"))
    check("with no Discord ID invented for it", req and req.discord_id is None)
    request_id = req.id
client.cookies.clear()
client.cookies.set("cta_session", auth._make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{request_id}/provision",
                json={"slug": "grove", "region": "Yorkshire & the Humber"})
check("provisioning succeeds", r.status_code == 200, r.text[:200])
with Session(database.engine) as db:
    ident = identity.find_identity(db, "google", "g-org")
    owner = db.get(User, ident.user_id) if ident else None
    check("the requester has an account reached through their identity", owner is not None)
    check("with no Discord ID", owner and owner.discord_id is None)
    check("and is the new club's super-admin", owner and owner.is_super_admin is True)


print("\n9. A legacy Discord club request still provisions")
with Session(database.engine) as db:
    db.add(ClubRequest(requester_name="Lee", requester_email="lee@x.test", club_name="Legacy Hall",
                       club_location="York", discord_id="d-legacy", discord_name="Lee#1"))
    db.commit()
    legacy_id = db.exec(select(ClubRequest).where(ClubRequest.club_name == "Legacy Hall")).first().id
r = client.post(f"/admin/platform/club-requests/{legacy_id}/provision",
                json={"slug": "legacy-hall", "region": "Yorkshire & the Humber"})
check("provisioning a pre-migration request succeeds", r.status_code == 200, r.text[:200])
with Session(database.engine) as db:
    ident = identity.find_identity(db, "discord", "d-legacy")
    check("its Discord identity became an account with an identity row", ident is not None)
    owner = db.get(User, ident.user_id) if ident else None
    check("and the mirror is written", owner and owner.discord_id == "d-legacy")


print("\n10. /auth/account (Slab 2) shows only the signed-in account's own data")
client.cookies.clear()
check("needs a session", client.get("/auth/account").status_code == 401)
with Session(database.engine) as db:
    admin = db.get(User, 1)
    db.add(Club(id=7, name="Away Club", slug="away"))
    db.add(Player(id=70, name="Joel Away", club_id=7, user_id=1, active=False))
    db.add(Player(id=71, name="Someone Else", club_id=7, user_id=2))
    db.commit()
    session_v = admin.session_version or 0
client.cookies.set("cta_session", auth._make_session_cookie(1, session_v))
acct = client.get("/auth/account").json()
check("the account, in the explicit shape", acct["user"]["id"] == 1 and "session_version" not in acct["user"])
provs = [(i["provider"], i["name"], i["is_primary"]) for i in acct["identities"]]
check("both Discord identities, primary first",
      [p[2] for p in provs] == [True, False] and all(p[0] == "discord" for p in provs), str(provs))
check("its own players at every club, archived ones included",
      sorted((p["name"], p["active"], p["club"]["slug"]) for p in acct["players"]) == [("Joel Away", False, "away")],
      str(acct["players"]))
check("nothing to add while Discord is the only sign-in method", acct["can_add"] == [])
auth.GOOGLE_CLIENT_ID, auth.GOOGLE_CLIENT_SECRET = "gid", "gsecret"
check("the nudge appears by itself once another method exists",
      client.get("/auth/account").json()["can_add"] == ["google"])
auth.GOOGLE_CLIENT_ID, auth.GOOGLE_CLIENT_SECRET = "", ""


print("\n11. A name the user owns (Slab 3)")
client.cookies.clear()
with Session(database.engine) as db:
    admin = db.get(User, 1)
    client.cookies.set("cta_session", auth._make_session_cookie(1, admin.session_version or 0))
r = client.patch("/auth/account", json={"display_name": "  Joel   the   Organiser "})
check("setting a name succeeds", r.status_code == 200, r.text[:150])
check("whitespace is tidied", r.json()["user"]["name"] == "Joel the Organiser", r.text[:150])
check("/auth/me greets by it", client.get("/auth/me").json()["user"]["name"] == "Joel the Organiser")
check("a name over the limit is refused with a reason",
      client.patch("/auth/account", json={"display_name": "x" * 33}).status_code == 422)
check("so is one with invisible characters",
      client.patch("/auth/account", json={"display_name": "Jo​el"}).status_code == 422)
check("32 characters is fine", client.patch("/auth/account", json={"display_name": "y" * 32}).status_code == 200)
client.patch("/auth/account", json={"display_name": "Joel the Organiser"})
with Session(database.engine) as db:
    resp = auth._finish_sign_in(db, ProviderProfile(provider="discord", subject="d-admin", name="Discord Changed"), None, None)
    u = db.get(User, 1)
    check("a Discord sign-in afterwards leaves it alone",
          (u.display_name, u.discord_name) == ("Joel the Organiser", "Discord Changed"),
          str((u.display_name, u.discord_name)))
    database.log_audit(db, u, "test.action")
    db.commit()
    from models import AuditLogEntry
    last = db.exec(select(AuditLogEntry).order_by(AuditLogEntry.id.desc())).first()
    check("the audit log records the name they go by", last.actor_name == "Joel the Organiser", last.actor_name)
with Session(database.engine) as db:
    u = db.get(User, 1)
    u.is_super_admin = True
    db.add(u)
    db.commit()
roles = client.get("/admin/roles", headers={"origin": "https://home.calltoarms.app"})
sas = roles.json().get("super_admins", []) if roles.status_code == 200 else []
check("admin lists carry the name beside the handle",
      [(x.get("name"), x.get("discord_name")) for x in sas] == [("Joel the Organiser", "Discord Changed")],
      roles.text[:200])
found = client.get("/admin/platform/users/search", params={"q": "Organiser"})
check("support search finds someone by the name they chose",
      found.status_code == 200 and any(x["user_id"] == 1 for x in found.json()), found.text[:200])
r = client.patch("/auth/account", json={"display_name": "   "})
check("clearing it goes back to the Discord handle", r.json()["user"]["name"] == "Discord Changed", r.text[:150])


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
