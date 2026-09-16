"""Account overhaul Slab 5: signing in, and adding an address, with an email link.

Sending is stubbed (email_login.send_link captures the link). What these assert
is the security shape described in email_login.py:

  * off unless EMAIL_SIGNIN says otherwise; "link" before "open"
  * the answer to "email me a link" is the same whether or not an account exists
  * only a hash of the token is stored; a token works once, for 15 minutes
  * per-address and per-IP limits, using Fly's client IP header
  * adding an address completes only in the browser signed in to that account,
    and opening it anywhere else doesn't spend it
  * links only ever point back at a calltoarms.app origin
  * a new email account is offered the existing account a verified email is on

Run: PYTHONPATH=. python tests/test_email_sign_in.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta
import re
from urllib.parse import parse_qs, urlparse

_DB = pathlib.Path(tempfile.mkdtemp()) / "email.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-email")
os.environ["RESEND_API_KEY"] = "test-key"
os.environ["EMAIL_FROM"] = "notifications@calltoarms.app"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
import email_login  # noqa: E402
import main  # noqa: E402
import user_merge  # noqa: E402
from identity import ProviderProfile, attach_identity  # noqa: E402
from models import Club, LoginToken, User, UserIdentity  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="Home", slug="home"))
    joel = User(id=1, discord_id="d-joel", discord_name="Joel", club_id=1, home_club_id=1)
    pat = User(id=2, discord_id="d-pat", discord_name="Pat", club_id=1, home_club_id=1)
    db.add(joel)
    db.add(pat)
    db.flush()
    attach_identity(db, joel, ProviderProfile(provider="discord", subject="d-joel", name="Joel"))
    attach_identity(db, pat, ProviderProfile(provider="discord", subject="d-pat", name="Pat"))
    attach_identity(db, pat, ProviderProfile(provider="google", subject="g-pat", name="Pat",
                                             email="pat@example.com", email_verified=True))
    db.commit()

SENT = []
email_login.send_link = lambda email, purpose, url: SENT.append((email, purpose, url))

ORIGIN = {"origin": "https://home.calltoarms.app"}


def client_for(user_id=None, ip="203.0.113.1"):
    c = TestClient(main.app, follow_redirects=False, headers={**ORIGIN, "fly-client-ip": ip})
    if user_id:
        c.cookies.set("cta_session", auth._make_session_cookie(user_id))
    return c


def token_from(url):
    return parse_qs(urlparse(url).query)["token"][0]


print("\n1. Off unless switched on")
auth.EMAIL_SIGNIN = "off"
check("sign-in screens don't offer email", client_for().get("/auth/me").json()["sign_in_providers"] == ["discord"])
check("starting an email sign-in is closed", client_for().post("/auth/email/start", json={"email": "a@b.co"}).status_code == 404)
check("/account doesn't offer adding one", "email" not in client_for(1).get("/auth/account").json()["can_add"])


print("\n2. EMAIL_SIGNIN=link: adding an address from /account")
auth.EMAIL_SIGNIN = "link"
check("/account offers it", "email" in client_for(1).get("/auth/account").json()["can_add"])
check("but it's not a sign-in method yet", client_for().post("/auth/email/start", json={"email": "a@b.co"}).status_code == 404)
r = client_for(1).post("/auth/email/link", json={"email": "  Joel@Example.COM "})
check("asking for a confirmation link succeeds", r.status_code == 200, r.text[:120])
email, purpose, url = SENT[-1]
check("sent to the address, tidied", (email, purpose) == ("joel@example.com", "link"), str(SENT[-1]))
check("the link opens our Continue page on the club it came from",
      url.startswith("https://home.calltoarms.app/signin/email?token="), url)
tok = token_from(url)
with Session(database.engine) as db:
    row = db.exec(select(LoginToken)).first()
    check("only a hash of the token is stored", row.token_hash != tok and tok not in (row.token_hash or ""))
r = client_for().post("/auth/email/verify", json={"token": tok})
check("opening it signed out is refused", r.status_code == 403, r.text[:120])
r = client_for(2).post("/auth/email/verify", json={"token": tok})
check("so is opening it signed in as someone else", r.status_code == 403, r.text[:120])
r = client_for(1).post("/auth/email/verify", json={"token": tok})
check("neither of those spent it: the right browser still can",
      r.status_code == 200 and r.json()["redirect"] == "https://home.calltoarms.app/account?linked=email", r.text[:150])
with Session(database.engine) as db:
    ident = db.exec(select(UserIdentity).where(UserIdentity.provider == "email")).first()
    check("the address is on Joel's account, verified",
          ident and (ident.user_id, ident.provider_user_id, ident.email_verified) == (1, "joel@example.com", True))
check("the same link doesn't work twice", client_for(1).post("/auth/email/verify", json={"token": tok}).status_code == 410)
client_for(2).post("/auth/email/link", json={"email": "joel@example.com"})
r = client_for(2).post("/auth/email/verify", json={"token": token_from(SENT[-1][2])})
check("confirming an address already on another account offers a merge instead of moving it",
      r.json().get("redirect") == "https://home.calltoarms.app/account?merge=pending&provider=email", r.text[:150])


print("\n3. EMAIL_SIGNIN=open: signing in")
auth.EMAIL_SIGNIN = "open"
check("sign-in screens offer email", "email" in client_for().get("/auth/me").json()["sign_in_providers"])
known = client_for(ip="198.51.100.7").post("/auth/email/start", json={"email": "joel@example.com", "next": "/signup"})
unknown = client_for(ip="198.51.100.7").post("/auth/email/start", json={"email": "nobody@example.com"})
check("the answer is identical for an address with an account and one without",
      known.status_code == unknown.status_code == 200 and known.json() == unknown.json(), f"{known.text} / {unknown.text}")
check("a non-address is refused", client_for().post("/auth/email/start", json={"email": "not an email"}).status_code == 422)
joel_link = next(u for e, p, u in reversed(SENT) if e == "joel@example.com" and p == "sign_in")
r = client_for().post("/auth/email/verify", json={"token": token_from(joel_link)})
cookies = " ".join(r.headers.get_list("set-cookie"))
check("Joel's link signs him in and returns him to where he was",
      r.status_code == 200 and r.json()["redirect"] == "https://home.calltoarms.app/signup"
      and re.search(r"cta_session=1:\d+:\d+\.", cookies) is not None,
      f"{r.text[:120]} {cookies[:80]}")

new_link = next(u for e, p, u in reversed(SENT) if e == "nobody@example.com")
r = client_for().post("/auth/email/verify", json={"token": token_from(new_link)})
check("a new address goes to the club picker", r.json().get("redirect") == "https://home.calltoarms.app/join", r.text[:150])
pending = next(h.split(";")[0].split("=", 1)[1] for h in r.headers.get_list("set-cookie") if h.startswith("cta_pending_signup="))
c = client_for()
c.cookies.set("cta_pending_signup", pending)
r = c.post("/auth/complete-signup", json={"club_id": 1})
with Session(database.engine) as db:
    ident = db.exec(select(UserIdentity).where(UserIdentity.provider_user_id == "nobody@example.com")).first()
    new_id = ident.user_id if ident else None
check("and becomes an account with that address", r.status_code == 200 and new_id is not None, r.text[:120])
r = client_for(new_id).post("/auth/create-profile", json={"name": "Nobody Special"}, headers=ORIGIN)
with Session(database.engine) as db:
    check("with no other name, its first roster name becomes its account name",
          r.status_code == 200 and db.get(User, new_id).display_name == "Nobody Special", r.text[:120])

client_for(ip="198.51.100.9").post("/auth/email/start", json={"email": "pat@example.com"})
r = client_for().post("/auth/email/verify", json={"token": token_from(SENT[-1][2])})
check("a new email sign-in whose address is verified on Pat's Google is offered Pat's account, not joined to it",
      r.json().get("redirect") == "https://home.calltoarms.app/join?existing_account=1", r.text[:150])

client_for(ip="198.51.100.10").post("/auth/email/start", json={"email": "late@example.com"})
late = token_from(SENT[-1][2])
with Session(database.engine) as db:
    row = db.exec(select(LoginToken).where(LoginToken.email == "late@example.com")).first()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.add(row)
    db.commit()
check("an expired link doesn't work", client_for().post("/auth/email/verify", json={"token": late}).status_code == 410)
check("nor does a made-up one", client_for().post("/auth/email/verify", json={"token": "x" * 43}).status_code == 410)


print("\n4. Limits, and where links point")
codes = [client_for(ip=f"192.0.2.{i}").post("/auth/email/start", json={"email": "flood@example.com"}).status_code
         for i in range(email_login.PER_EMAIL_LIMIT + 1)]
check(f"one address gets {email_login.PER_EMAIL_LIMIT} links an hour, then a wait",
      codes[:-1] == [200] * email_login.PER_EMAIL_LIMIT and codes[-1] == 429, str(codes))
codes = [client_for(ip="192.0.2.200").post("/auth/email/start", json={"email": f"p{i}@example.com"}).status_code
         for i in range(email_login.PER_IP_LIMIT + 1)]
check(f"one IP gets {email_login.PER_IP_LIMIT} an hour across addresses, then a wait",
      codes[:-1] == [200] * email_login.PER_IP_LIMIT and codes[-1] == 429, str(codes[-3:]))
check("a different IP is unaffected",
      client_for(ip="192.0.2.201").post("/auth/email/start", json={"email": "p999@example.com"}).status_code == 200)
evil = TestClient(main.app, headers={"origin": "https://evil.example", "fly-client-ip": "192.0.2.250"})
evil.post("/auth/email/start", json={"email": "target@example.com"})
check("a link requested from another site points at our own frontend, never theirs",
      not SENT[-1][2].startswith("https://evil.example"), SENT[-1][2])
check("an account merge knows about login_tokens.user_id", user_merge.unhandled_user_columns() == [])


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
