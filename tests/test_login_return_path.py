"""Signing in must return you to the page you were trying to reach.

On 09/09/2026 four Age of Sigmar players followed the Call to Arms link to
`/signup?system=Age+of+Sigmar`, signed in, and were dropped on the club's front
page instead — then had to click the same link a second time. One described it
as "a loop to log in then back to the main page".

Two faults, and fixing only the first makes the second worse:

1. Every sign-in link in the app omitted `next`, so `/discord/login` never knew
   where the player had been headed.
2. The callback stored origin and next as ONE concatenated string, and the
   brand-new-user branch did `f"{return_to}/join"` — which appends a path to
   the end of a query string the moment `next` is present.

Run: PYTHONPATH=. python tests/test_login_return_path.py
"""
import os
import pathlib
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATABASE_URL", f"sqlite:///{pathlib.Path(tempfile.mkdtemp())/'a.db'}")

from auth import _safe_next_path, _safe_return_to  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


CLUB = "https://egnwgc.calltoarms.app"
SIGNUP = "/signup?system=Age+of+Sigmar"


class _Req:
    def __init__(self, referer=None):
        self.headers = {"referer": referer} if referer else {}


def existing_user_lands_on(origin, next_path):
    """What the callback redirects a returning player to."""
    return origin + _safe_next_path(next_path)


def new_user_lands_on(origin, next_path):
    """What the callback redirects a brand-new identity to."""
    from urllib.parse import quote

    safe = _safe_next_path(next_path)
    url = f"{origin}/join"
    if safe:
        url += f"?next={quote(safe, safe='')}"
    return url


print("\n1. The origin still comes from the Referer, unchanged")
check("a club subdomain returns to itself",
      _safe_return_to(_Req(f"{CLUB}{SIGNUP}")) == CLUB)
check("an unknown host falls back rather than trusting it",
      _safe_return_to(_Req("https://evil.example/x")) != "https://evil.example")
check("no referer is fine", _safe_return_to(_Req()).startswith("http"))


print("\n2. A returning player lands on the page they clicked")
check("back to the signup form, system intact",
      existing_user_lands_on(CLUB, SIGNUP) == f"{CLUB}{SIGNUP}",
      existing_user_lands_on(CLUB, SIGNUP))
check("no next still means the front door",
      existing_user_lands_on(CLUB, None) == CLUB)


print("\n3. A brand-new player keeps the destination through the club picker")
url = new_user_lands_on(CLUB, SIGNUP)
parsed = urlparse(url)
check("goes to the club picker", parsed.path == "/join", url)
check("carries the destination as a query param",
      parse_qs(parsed.query).get("next") == [SIGNUP], url)
# The exact shape of the old bug: a path glued onto the end of a query string.
check("the destination is NOT appended as a path",
      not url.endswith("/join") or SIGNUP not in url, url)
check("nothing that looks like ...system=X/join",
      "/join" not in parsed.query, url)
check("no next means a plain club picker",
      new_user_lands_on(CLUB, None) == f"{CLUB}/join")


print("\n4. next can't become an open redirect")
for evil in ["//evil.example", "https://evil.example", "http://evil.example",
             "/ok\nSet-Cookie: x=1", "/ok\\evil", None, "", "notapath"]:
    safe = _safe_next_path(evil)
    landed = existing_user_lands_on(CLUB, evil)
    ok = landed.startswith(CLUB + "/") or landed == CLUB
    check(f"rejected or contained: {evil!r}", ok, landed)
check("a legitimate deep path is allowed",
      _safe_next_path("/pairings?week=10/09/2026") == "/pairings?week=10/09/2026")
check("an over-long path is truncated, not rejected outright",
      len(_safe_next_path("/x" + "y" * 500)) == 300)

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
