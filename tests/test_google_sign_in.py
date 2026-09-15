"""Account overhaul Slab 4: Google, added to an account first, sign-in second.

Google is stubbed (auth._google_profile), so this covers everything of ours
around it: which settings show what, linking from /account, refusing to take a
Google account that belongs to someone else, a brand-new Google account, and
the Decision C offer when a verified email is already on an account.

Run: PYTHONPATH=. python tests/test_google_sign_in.py
"""
import os
import pathlib
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

_DB = pathlib.Path(tempfile.mkdtemp()) / "google.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-google")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import main  # noqa: E402
from identity import ProviderProfile, attach_identity  # noqa: E402
from models import AuditLogEntry, Club, Player, User, UserIdentity  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="Home", slug="home"))
    joel = User(id=1, discord_id="d-joel", discord_name="Joel", club_id=1, home_club_id=1)
    other = User(id=2, discord_id="d-other", discord_name="Other", club_id=1, home_club_id=1)
    db.add(joel)
    db.add(other)
    db.flush()
    attach_identity(db, joel, ProviderProfile(provider="discord", subject="d-joel", name="Joel"))
    attach_identity(db, other, ProviderProfile(provider="discord", subject="d-other", name="Other"))
    db.add(Player(id=10, name="Joel Kirk", club_id=1, user_id=1))
    db.commit()

GOOGLE = {}


async def fake_google_profile(code):
    return GOOGLE[code]


auth._google_profile = fake_google_profile


def configure(client_id="", signin="link"):
    auth.GOOGLE_CLIENT_ID = client_id
    auth.GOOGLE_CLIENT_SECRET = "secret" if client_id else ""
    auth.GOOGLE_SIGNIN = signin


def client_for(user_id=None, version=0):
    c = TestClient(main.app, follow_redirects=False)
    if user_id:
        c.cookies.set("cta_session", auth._make_session_cookie(user_id, version))
    return c


def callback(c, code, link_cookie=None):
    c.cookies.set("cta_oauth_state", "st")
    c.cookies.set("cta_oauth_return_to", "https://home.calltoarms.app")
    if link_cookie:
        c.cookies.set("cta_oauth_link", link_cookie)
    return c.get("/auth/google/callback", params={"state": "st", "code": code})


print("\n1. Not configured: Google is nowhere")
configure("")
anon = client_for()
check("sign-in screens offer Discord only", anon.get("/auth/me").json()["sign_in_providers"] == ["discord"])
check("the Google sign-in route is closed", anon.get("/auth/google/login").status_code == 404)
check("nothing to add on /account", client_for(1).get("/auth/account").json()["can_add"] == [])


print("\n2. Configured, GOOGLE_SIGNIN=link: only adding it from /account")
configure("gid", "link")
check("sign-in screens still offer Discord only", anon.get("/auth/me").json()["sign_in_providers"] == ["discord"])
check("so the Google sign-in route stays closed", anon.get("/auth/google/login").status_code == 404)
check("/account offers adding Google", client_for(1).get("/auth/account").json()["can_add"] == ["google"])
r = anon.get("/auth/google/link", headers={"referer": "https://home.calltoarms.app/account"})
check("starting a link signed out just goes back to /account",
      r.status_code in (302, 307) and r.headers["location"] == "https://home.calltoarms.app/account", r.headers.get("location"))
joel_c = client_for(1)
r = joel_c.get("/auth/google/link", headers={"referer": "https://home.calltoarms.app/account"})
loc = urlparse(r.headers.get("location", ""))
check("signed in, it goes to Google's account picker",
      loc.netloc == "accounts.google.com" and parse_qs(loc.query).get("prompt") == ["select_account"]
      and parse_qs(loc.query).get("scope") == ["openid email profile"], r.headers.get("location"))
set_cookies = " ".join(r.headers.get_list("set-cookie"))
check("and remembers which account it's linking to", "cta_oauth_link=" in set_cookies)


print("\n3. Linking Google to Joel's account")
GOOGLE["joel"] = ProviderProfile(provider="google", subject="g-joel", name="Joel K",
                                 avatar_url="https://lh3.googleusercontent.com/x", email="joel@example.com",
                                 email_verified=True)
with Session(database.engine) as db:
    link = auth._link_cookie_value(db.get(User, 1))
r = callback(client_for(1), "joel", link)
check("comes back to /account saying it worked",
      r.headers.get("location") == "https://home.calltoarms.app/account?linked=google", r.headers.get("location"))
with Session(database.engine) as db:
    g = db.exec(select(UserIdentity).where(UserIdentity.provider == "google")).first()
    check("the Google identity is on Joel's account", g is not None and g.user_id == 1)
    check("with its verified email kept for recovery and matching",
          g and (g.email, g.email_verified, g.is_primary) == ("joel@example.com", True, True))
    check("his Discord account is still the one that gets mentioned", db.get(User, 1).discord_id == "d-joel")
    check("the link is in the audit log",
          db.exec(select(AuditLogEntry).where(AuditLogEntry.action == "identity.link")).first() is not None)
check("/account has nothing left to add", client_for(1).get("/auth/account").json()["can_add"] == [])

with Session(database.engine) as db:
    link_other = auth._link_cookie_value(db.get(User, 2))
r = callback(client_for(2), "joel", link_other)
check("someone else linking the same Google account is offered a merge, not given it",
      r.headers.get("location") == "https://home.calltoarms.app/account?merge=pending&provider=google",
      r.headers.get("location"))
with Session(database.engine) as db:
    check("and it stays where it was", db.exec(select(UserIdentity).where(UserIdentity.provider_user_id == "g-joel")).first().user_id == 1)

r = callback(client_for(1), "joel", link_other)
check("a link cookie for a different account than the one signed in does nothing",
      "link_error=signed_out" in r.headers.get("location", ""), r.headers.get("location"))
r = client_for(2)
r.cookies.set("cta_oauth_state", "st")
r.cookies.set("cta_oauth_return_to", "https://home.calltoarms.app")
r.cookies.set("cta_oauth_link", link_other)
resp = r.get("/auth/google/callback", params={"state": "st", "error": "access_denied"})
check("backing out on Google's screen comes back cancelled",
      "link_error=cancelled" in resp.headers.get("location", ""), resp.headers.get("location"))


print("\n4. GOOGLE_SIGNIN=open: a sign-in button too")
configure("gid", "open")
check("sign-in screens offer Discord and Google", anon.get("/auth/me").json()["sign_in_providers"] == ["discord", "google"])
r = anon.get("/auth/google/login", params={"next": "/signup"}, headers={"referer": "https://home.calltoarms.app/signup"})
check("the Google sign-in route is open", urlparse(r.headers.get("location", "")).netloc == "accounts.google.com")
r = callback(client_for(), "joel")
check("Joel's linked Google signs him into his existing account",
      r.headers.get("location") == "https://home.calltoarms.app" and "cta_session=1." in " ".join(r.headers.get_list("set-cookie")),
      str(r.headers.get_list("set-cookie"))[:200])

GOOGLE["new"] = ProviderProfile(provider="google", subject="g-new", name="Gail Google",
                                email="gail@example.com", email_verified=True)
r = callback(client_for(), "new")
check("a new Google person goes to the club picker, no offer", r.headers.get("location") == "https://home.calltoarms.app/join",
      r.headers.get("location"))
pending = next(h.split(";")[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie") if h.startswith("cta_pending_signup="))
c = client_for()
c.cookies.set("cta_pending_signup", pending)
r = c.post("/auth/complete-signup", json={"club_id": 1})
check("and becomes an account", r.status_code == 200, r.text[:150])
with Session(database.engine) as db:
    ident = db.exec(select(UserIdentity).where(UserIdentity.provider_user_id == "g-new")).first()
    gail = db.get(User, ident.user_id) if ident else None
    check("with no Discord", gail and (gail.discord_id, gail.discord_name) == (None, None))
    check("going by the name Google gave, which is hers to change", gail and gail.display_name == "Gail Google")
    db.add(Player(id=20, name="Gail", club_id=1, user_id=gail.id))
    db.commit()
    gail_id = gail.id
check("the app greets her by it", client_for(gail_id).get("/auth/me").json()["user"]["name"] == "Gail Google")
prof = client_for(gail_id).get("/players/20", headers={"origin": "https://home.calltoarms.app"})
check("her profile page has no empty Discord chip",
      prof.status_code == 200 and prof.json().get("discord") is None, f"{prof.status_code} {prof.text[:150]}")


print("\n5. Decision C: a verified email already on an account is offered, never joined")
GOOGLE["dupe"] = ProviderProfile(provider="google", subject="g-second-joel", name="Joel again",
                                 email="JOEL@example.com", email_verified=True)
r = callback(client_for(), "dupe")
check("the club picker is told an account with that email exists",
      r.headers.get("location") == "https://home.calltoarms.app/join?existing_account=1", r.headers.get("location"))
with Session(database.engine) as db:
    check("and nothing was joined or created",
          db.exec(select(UserIdentity).where(UserIdentity.provider_user_id == "g-second-joel")).first() is None)
GOOGLE["unverified"] = ProviderProfile(provider="google", subject="g-unverified", name="Maybe Joel",
                                       email="joel@example.com", email_verified=False)
r = callback(client_for(), "unverified")
check("an unverified email gets no offer", r.headers.get("location") == "https://home.calltoarms.app/join",
      r.headers.get("location"))


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
