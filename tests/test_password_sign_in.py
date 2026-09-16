"""Account overhaul Slab 8: an account of our own, with a password.

Sending is stubbed; Argon2 and the strength rules are real, but the breach
check is stubbed so the suite doesn't depend on someone else's API.

What these assert:
  * off unless PASSWORD_SIGNIN says so
  * signing up creates NOTHING until the address is confirmed, and says the
    same whether or not the address already has an account
  * only an Argon2id hash is stored, never the password
  * wrong passwords are refused with one message, and rate limited per address
    and per IP
  * a forgotten password can be reset from an emailed link, which ends every
    other session
  * changing a password needs the current one and ends other sessions
  * removing the password method takes the password with it

Run: PYTHONPATH=. python tests/test_password_sign_in.py
"""
import os
import pathlib
import sys
import tempfile
import re
from urllib.parse import parse_qs, urlparse

_DB = pathlib.Path(tempfile.mkdtemp()) / "password.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-passwords")
os.environ["RESEND_API_KEY"] = "test-key"
os.environ["EMAIL_FROM"] = "notifications@calltoarms.app"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import email_login  # noqa: E402
import main  # noqa: E402
import passwords  # noqa: E402
from identity import ProviderProfile, attach_identity  # noqa: E402
from models import (  # noqa: E402
    AuditLogEntry, Club, LoginAttempt, LoginToken, PasswordCredential, User, UserIdentity,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


GOOD = "kZ7-quiet-otter-plinth"
ALSO_GOOD = "another-perfectly-fine-one-42"

# The real HIBP call is the one thing here that would reach the internet.
BREACHED = {"password123456": 1, "letmein12345": 1}
passwords.breach_count = lambda pw: BREACHED.get(pw, 0)

SENT = []
email_login.send_link = lambda email, purpose, url: SENT.append((email, purpose, url))
NOTICES = []
email_login.send_existing_account_notice = lambda email, url: NOTICES.append((email, url))

SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="Home", slug="home"))
    joel = User(id=1, discord_id="d-joel", discord_name="Joel", club_id=1, home_club_id=1)
    db.add(joel)
    db.flush()
    attach_identity(db, joel, ProviderProfile(provider="discord", subject="d-joel", name="Joel"))
    attach_identity(db, joel, ProviderProfile(provider="google", subject="g-joel", name="Joel",
                                              email="joel@example.com", email_verified=True))
    db.commit()

ORIGIN = {"origin": "https://home.calltoarms.app"}


def client(user_id=None, ip="203.0.113.5", version=0):
    c = TestClient(main.app, follow_redirects=False, headers={**ORIGIN, "fly-client-ip": ip})
    if user_id:
        c.cookies.set("cta_session", auth._make_session_cookie(user_id, version))
    return c


def token_from(url):
    return parse_qs(urlparse(url).query)["token"][0]


def version_of(user_id):
    with Session(database.engine) as db:
        return db.get(User, user_id).session_version or 0


print("\n1. Off unless switched on")
auth.PASSWORD_SIGNIN = "off"
check("not a sign-in method", "password" not in client().get("/auth/me").json()["sign_in_providers"])
check("signing up is closed",
      client().post("/auth/password/signup", json={"email": "a@b.co", "password": GOOD}).status_code == 404)
check("signing in is closed",
      client().post("/auth/password/signin", json={"email": "a@b.co", "password": GOOD}).status_code == 404)


print("\n2. Signing up creates nothing until the address is confirmed")
auth.PASSWORD_SIGNIN = "open"
check("a weak password is refused with a reason",
      client().post("/auth/password/signup", json={"email": "new@example.com", "password": "short"}).status_code == 422)
r = client().post("/auth/password/signup", json={"email": "new@example.com", "password": "password123456"})
check("so is one from a known breach", r.status_code == 422 and "breach" in r.json()["detail"], r.text[:120])
r = client().post("/auth/password/signup", json={"email": " New@Example.com ", "password": GOOD, "next": "/signup"})
check("a good one is accepted", r.status_code == 200, r.text[:150])
with Session(database.engine) as db:
    check("no account yet", db.exec(select(User).where(User.id > 1)).all() == [])
    row = db.exec(select(LoginToken).where(LoginToken.purpose == "password_signup")).first()
    check("the password waits as an Argon2 hash, not as itself",
          row and row.secret and row.secret.startswith("$argon2id$") and GOOD not in row.secret)
confirm = next(u for e, p, u in SENT if e == "new@example.com" and p == "password_signup")
r = client().post("/auth/email/verify", json={"token": token_from(confirm)})
check("confirming sends them on to pick a club", r.json().get("redirect", "").endswith("/join?next=%2Fsignup"),
      r.text[:150])
pending = next(h.split(";")[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie") if h.startswith("cta_pending_signup="))
c = client()
c.cookies.set("cta_pending_signup", pending)
r = c.post("/auth/complete-signup", json={"club_id": 1})
check("and the account is created", r.status_code == 200, r.text[:150])
with Session(database.engine) as db:
    ident = db.exec(select(UserIdentity).where(UserIdentity.provider == "password")).first()
    new_id = ident.user_id if ident else None
    cred = db.exec(select(PasswordCredential).where(PasswordCredential.user_id == new_id)).first() if new_id else None
    check("with the address confirmed", ident and (ident.provider_user_id, ident.email_verified) == ("new@example.com", True))
    check("and the password it was signed up with, as a hash",
          cred and cred.hash.startswith("$argon2id$") and passwords.verify_password(cred.hash, GOOD)[0])
    check("the hash is no longer left on the token row",
          db.exec(select(LoginToken).where(LoginToken.purpose == "password_signup")).first().secret is None)


print("\n3. Signing up with an address that already has an account")
before = len(SENT)
r = client().post("/auth/password/signup", json={"email": "new@example.com", "password": ALSO_GOOD})
check("answers exactly as it does for a new address", r.status_code == 200 and r.json() == auth._SIGNUP_SENT, r.text[:150])
check("no confirmation link is sent", len(SENT) == before)
check("the owner is told by email instead", NOTICES and NOTICES[-1][0] == "new@example.com", str(NOTICES[-1:]))
r = client().post("/auth/password/signup", json={"email": "joel@example.com", "password": ALSO_GOOD})
check("same for an address confirmed on another account (Joel's Google)",
      r.status_code == 200 and NOTICES[-1][0] == "joel@example.com")


print("\n4. Signing in")
r = client().post("/auth/password/signin", json={"email": "new@example.com", "password": "wrong-one-entirely"})
check("a wrong password is refused", r.status_code == 401 and r.json()["detail"] == auth._WRONG, r.text[:120])
r = client().post("/auth/password/signin", json={"email": "nobody@example.com", "password": GOOD})
check("an unknown address gets the same words", r.status_code == 401 and r.json()["detail"] == auth._WRONG)
r = client().post("/auth/password/signin", json={"email": "new@example.com", "password": GOOD, "next": "/pairings"})
check("the right one signs them in, where they were headed",
      r.status_code == 200 and r.json()["redirect"] == "https://home.calltoarms.app/pairings"
      and re.search(rf"cta_session={new_id}:\d+:\d+\.", " ".join(r.headers.get_list("set-cookie"))) is not None,
      r.text[:150])

for i in range(auth.PASSWORD_FAIL_LIMIT_EMAIL):
    client(ip=f"198.51.100.{i}").post("/auth/password/signin", json={"email": "new@example.com", "password": "nope-nope-nope"})
r = client(ip="198.51.100.200").post("/auth/password/signin", json={"email": "new@example.com", "password": GOOD})
check(f"after {auth.PASSWORD_FAIL_LIMIT_EMAIL} wrong tries that address waits, even with the right password",
      r.status_code == 429, r.text[:120])
r = client(ip="198.51.100.201").post("/auth/password/signin", json={"email": "other@example.com", "password": GOOD})
check("another address is unaffected", r.status_code == 401, r.text[:120])
with Session(database.engine) as db:
    rows = db.exec(select(LoginAttempt)).all()
    check("attempts are kept as keyed hashes, never the address",
          rows and all("new@example.com" not in r.key_hash for r in rows))


print("\n5. A forgotten password")
r = client().post("/auth/password/forgot", json={"email": "nobody@example.com"})
r2 = client().post("/auth/password/forgot", json={"email": "new@example.com"})
check("the answer is the same whether or not there's an account",
      r.status_code == r2.status_code == 200 and r.json() == r2.json())
reset_link = next(u for e, p, u in reversed(SENT) if p == "password_reset")
check("the link points at the reset page", "/signin/reset?token=" in reset_link, reset_link)
before_version = version_of(new_id)
r = client().post("/auth/password/reset", json={"token": token_from(reset_link), "password": "letmein12345"})
check("a breached new password is refused", r.status_code == 422, r.text[:120])
r = client().post("/auth/password/reset", json={"token": token_from(reset_link), "password": ALSO_GOOD})
check("a good one is accepted and signs them in",
      r.status_code == 200 and f"cta_session={new_id}:" in " ".join(r.headers.get_list("set-cookie")), r.text[:150])
check("every other session is ended", version_of(new_id) == before_version + 1)
check("the same link can't be used again",
      client().post("/auth/password/reset", json={"token": token_from(reset_link), "password": GOOD}).status_code == 410)
r = client(ip="198.51.100.250").post("/auth/password/signin", json={"email": "new@example.com", "password": ALSO_GOOD})
check("the new password works", r.status_code == 200, r.text[:120])
check("and the old one doesn't",
      client(ip="198.51.100.251").post("/auth/password/signin",
                                       json={"email": "new@example.com", "password": GOOD}).status_code == 401)
with Session(database.engine) as db:
    check("the reset is in the audit log",
          db.exec(select(AuditLogEntry).where(AuditLogEntry.action == "password.reset")).first() is not None)


print("\n6. Setting and changing one from /account")
acct = client(1, version=version_of(1)).get("/auth/account").json()
check("an account without one is offered it", "password" in acct["can_add"] and acct["has_password"] is False)
r = client(1, version=version_of(1)).post("/auth/password/set", json={"password": GOOD})
check("Joel can set one, because his Google address is confirmed", r.status_code == 200, r.text[:150])
acct = client(1, version=version_of(1)).get("/auth/account").json()
check("it shows as a way in, on his confirmed address",
      acct["has_password"] is True
      and any(i["provider"] == "password" and i["email"] == "joel@example.com" for i in acct["identities"]),
      str(acct["identities"]))
r = client(1, version=version_of(1)).post("/auth/password/set", json={"password": ALSO_GOOD})
check("changing it without the current one is refused", r.status_code == 403, r.text[:120])
v = version_of(1)
r = client(1, version=v).post("/auth/password/set",
                              json={"password": ALSO_GOOD, "current_password": GOOD})
check("with the current one it changes", r.status_code == 200, r.text[:150])
check("and other sessions are ended", version_of(1) == v + 1)
check("the old session cookie is dead", client(1, version=v).get("/auth/me").json().get("authenticated") is False)

with Session(database.engine) as db:
    pw_ident = db.exec(select(UserIdentity).where(UserIdentity.user_id == 1)
                       .where(UserIdentity.provider == "password")).first().id
r = client(1, version=version_of(1)).delete(f"/auth/identities/{pw_ident}")
with Session(database.engine) as db:
    check("removing the password method takes the password with it",
          r.status_code == 200
          and db.exec(select(PasswordCredential).where(PasswordCredential.user_id == 1)).first() is None,
          r.text[:120])


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
