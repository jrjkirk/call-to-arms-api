"""Requesting a club, and provisioning it in one click.

The old flow was seven steps and three rounds of email, and verified nothing:
the form was anonymous, so any name could claim any club at any address, and
the email was never checked. Worse, the requester could not be made their own
club's admin until after the club existed, because `users.club_id` is NOT NULL
and `/join` only lists clubs that already exist — so a new organiser could not
hold an account at the moment they most needed one.

What these assert:
  * a request without a Discord identity is refused
  * a HALF-finished sign-in counts, because that is all a new organiser can have
  * provisioning creates the club, the user, the super-admin grant and the
    club's systems together
  * the reviewer keeps the ability to hold any of that back

Run: PYTHONPATH=. python tests/test_club_onboarding.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "onboarding.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-onboarding")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
import main  # noqa: E402
from auth import _make_pending_signup_cookie, _make_session_cookie  # noqa: E402
from models import Club, ClubRequest, ClubSystem, SystemConfig, User  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(SystemConfig(id=1, name="The Old World", slug="tow",
                        legacy_system_name="The Old World", active=True))
    db.add(SystemConfig(id=2, name="Kill Team", slug="kt",
                        legacy_system_name="Kill Team", active=True))
    # A platform admin needs a club to belong to; it is not the club under test.
    db.add(Club(id=1, name="Existing Club", slug="existing"))
    db.add(User(id=1, discord_id="admin-1", discord_name="Joel", club_id=1,
                is_platform_admin=True))
    db.commit()

client = TestClient(main.app)

REQUEST = {
    "requester_name": "Nick",
    "requester_email": "nick@badmoon.test",
    "club_name": "Badmoon Bunker",
    "club_location": "Sheffield",
    "region": "Yorkshire & the Humber",
    "preferred_slug": "badmoon",
    "systems": ["The Old World", "Kill Team"],
    "club_night_day": "Thursday",
    "club_night_time": "18:00",
    "player_count": 24,
    "requester_role": "Club organiser",
    "evidence_url": "https://discord.gg/badmoon",
    "notes": "We run weekly.",
}


print("\n1. An anonymous request is refused")
r = client.post("/club-requests", json=REQUEST)
check("401 without any Discord identity", r.status_code == 401, f"got {r.status_code}")
with Session(database.engine) as db:
    check("nothing was written", db.exec(select(ClubRequest)).first() is None)


print("\n2. A half-finished sign-in is enough — it has to be")
# This is the bootstrap case: a brand-new organiser has a verified Discord
# identity but cannot yet have a User row, because the club it would belong to
# is the one they are asking for.
client.cookies.set("cta_pending_signup",
                   _make_pending_signup_cookie("discord-nick", "Nick#1234", None))
r = client.post("/club-requests", json=REQUEST)
check("accepted with only the pending-signup cookie", r.status_code == 201, r.text[:120])
with Session(database.engine) as db:
    req = db.exec(select(ClubRequest)).first()
    check("the Discord identity is recorded", req and req.discord_id == "discord-nick")
    check("so is the handle, for a human to look up", req.discord_name == "Nick#1234")
    check("no user id, because there is no user yet", req.requester_user_id is None)
    check("the systems came through", req.systems == ["The Old World", "Kill Team"])
    check("so did the club night", (req.club_night_day, req.club_night_time) == ("Thursday", "18:00"))
    check("and the evidence link", req.evidence_url == "https://discord.gg/badmoon")
    request_id = req.id


print("\n3. The same person can't queue a second pending request")
r = client.post("/club-requests", json={**REQUEST, "club_name": "Second Try"})
check("409 on a duplicate pending request", r.status_code == 409, f"got {r.status_code}")


print("\n4. A bad region is rejected rather than silently dropped")
client.cookies.set("cta_pending_signup",
                   _make_pending_signup_cookie("discord-other", "Other", None))
r = client.post("/club-requests", json={**REQUEST, "region": "Narnia"})
check("422 on an unknown region", r.status_code == 422, f"got {r.status_code}")
client.cookies.delete("cta_pending_signup")


print("\n5. One click provisions the club, the admin and the systems")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{request_id}/provision",
                json={"slug": "badmoon", "region": "Yorkshire & the Humber"})
check("provision succeeded", r.status_code == 200, r.text[:200])
body = r.json() if r.status_code == 200 else {}
with Session(database.engine) as db:
    club = db.exec(select(Club).where(Club.slug == "badmoon")).first()
    check("the club exists", club is not None)
    check("named and addressed from the request",
          club and (club.name, club.address) == ("Badmoon Bunker", "Sheffield"))

    owner = db.exec(select(User).where(User.discord_id == "discord-nick")).first()
    check("the requester now has an account", owner is not None)
    check("attached to their new club", owner and owner.club_id == club.id)
    check("and is its super-admin", owner and owner.is_super_admin is True)
    check("the response names who was appointed",
          body.get("appointed_super_admin", {}).get("discord_name") == "Nick#1234", str(body.get("appointed_super_admin")))

    cs = db.exec(select(ClubSystem).where(ClubSystem.club_id == club.id)).all()
    check("both requested systems are switched on", len(cs) == 2, str(len(cs)))
    check("on the club night they gave us",
          all(c.session_day == "Thursday" for c in cs))
    check("the response lists them", sorted(body.get("enabled_systems", [])) ==
          ["Kill Team", "The Old World"], str(body.get("enabled_systems")))

    req = db.get(ClubRequest, request_id)
    check("the request is approved and linked",
          req.status == "approved" and req.provisioned_club_id == club.id)


print("\n6. Provisioning twice is refused")
r = client.post(f"/admin/platform/club-requests/{request_id}/provision", json={"slug": "badmoon2"})
check("409 on re-provision", r.status_code == 409, f"got {r.status_code}")


print("\n7. A reviewer can still hold things back")
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-zoe", "Zoe", None))
client.post("/club-requests", json={**REQUEST, "club_name": "Quiet Club",
                                    "preferred_slug": "quiet"})
with Session(database.engine) as db:
    quiet_id = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-zoe")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{quiet_id}/provision",
                json={"slug": "quiet", "appoint_super_admin": False, "enable_systems": False})
check("provision succeeded", r.status_code == 200, r.text[:160])
with Session(database.engine) as db:
    club = db.exec(select(Club).where(Club.slug == "quiet")).first()
    check("no super-admin was appointed",
          db.exec(select(User).where(User.discord_id == "discord-zoe")).first() is None)
    check("no systems were enabled",
          db.exec(select(ClubSystem).where(ClubSystem.club_id == club.id)).first() is None)
    check("but the club still exists", club is not None)

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
