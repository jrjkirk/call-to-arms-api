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
# sender() derives the per-purpose address from this domain.
os.environ.setdefault("EMAIL_FROM", "notifications@calltoarms.app")

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

# Capture instead of send. Nothing here should ever reach Resend, and the
# assertions below care about *what would have been sent*, not the transport.
SENT_EMAILS = []


def _fake_send(to, subject, html, cc=None, text=None, from_addr=None):
    SENT_EMAILS.append({"to": to, "subject": subject, "html": html, "text": text,
                        "from": from_addr})
    return "fake-message-id"


import club_emails  # noqa: E402

club_emails.send_email = _fake_send

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

print("\n8. The requester actually hears back")
# Silence was the single biggest source of onboarding back-and-forth: a request
# went in and nothing happened until someone remembered to write by hand.
ack = [e for e in SENT_EMAILS if "got your request" in e["subject"]]
check("an acknowledgement went out on submit", len(ack) >= 1, str([e["subject"] for e in SENT_EMAILS]))
check("addressed to the requester", ack and ack[0]["to"] == "nick@badmoon.test")
check("naming their club", ack and "Badmoon Bunker" in ack[0]["html"])

live = [e for e in SENT_EMAILS if "is live" in e["subject"]]
check("a welcome email went out on provision", len(live) >= 1)
check("with their own club address",
      live and "https://badmoon.calltoarms.app" in live[0]["html"], live[0]["html"][:200] if live else "")
check("telling them they're the owner", live and "as its owner" in live[0]["html"])
check("and what we switched on for them",
      live and "The Old World" in live[0]["html"] and "Thursday" in live[0]["html"])

print("\n9. A decline closes the loop instead of going silent")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-declined", "Sam", None))
client.cookies.delete("cta_session")
client.post("/club-requests", json={**REQUEST, "club_name": "Nope Club"})
with Session(database.engine) as db:
    nope_id = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-declined")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
before = len(SENT_EMAILS)
r = client.post(f"/admin/platform/club-requests/{nope_id}/deny",
                json={"reason": "We couldn't tell that you run this club."})
check("deny succeeded", r.status_code == 200, r.text[:120])
declined = [e for e in SENT_EMAILS[before:] if "About your" in e["subject"]]
check("a decline email went out", len(declined) == 1, str(len(declined)))
check("carrying the reason given",
      declined and "couldn&#x27;t tell that you run this club" in declined[0]["html"],
      declined[0]["html"][:200] if declined else "")

print("\n10. Spam can be declined silently")
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-spam", "Spam", None))
client.post("/club-requests", json={**REQUEST, "club_name": "Spam Club"})
with Session(database.engine) as db:
    spam_id = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-spam")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
before = len(SENT_EMAILS)
client.post(f"/admin/platform/club-requests/{spam_id}/deny", json={"notify": False})
check("nothing was sent when notify is false", len(SENT_EMAILS) == before)

print("\n11. A broken mailer must not lose a request or a club")
# The submission and the club are the records. Email is a courtesy on top, and
# an outage at Resend must never take either of them with it.
def _boom(*a, **k):
    raise RuntimeError("Resend is down")

club_emails.send_email = _boom
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-outage", "Ada", None))
r = client.post("/club-requests", json={**REQUEST, "club_name": "Outage Club", "preferred_slug": "outage"})
check("the request is still accepted", r.status_code == 201, r.text[:120])
with Session(database.engine) as db:
    outage = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-outage")).first()
    check("and recorded", outage is not None)
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{outage.id}/provision", json={"slug": "outage"})
check("the club is still created", r.status_code == 200, r.text[:160])
check("and the failure is reported back, not hidden",
      r.json().get("email", "").startswith("failed"), str(r.json().get("email")))
with Session(database.engine) as db:
    check("the club really exists",
          db.exec(select(Club).where(Club.slug == "outage")).first() is not None)
club_emails.send_email = _fake_send

print("\n11b. A request with no club night schedules nothing, rather than guessing")
# Guessing "Wednesday" published a specific wrong night on a club's public page
# and drove every schedule default from it. No systems reads as unfinished; a
# plausible wrong answer does not.
club_emails.send_email = _fake_send
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-nonight", "Pat", None))
client.post("/club-requests", json={**REQUEST, "club_name": "No Night Club",
                                    "preferred_slug": "nonight", "club_night_day": None,
                                    "club_night_time": None})
with Session(database.engine) as db:
    nn = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-nonight")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{nn}/provision", json={"slug": "nonight"})
check("provision succeeded", r.status_code == 200, r.text[:140])
check("no systems were enabled", r.json().get("enabled_systems") == [], str(r.json().get("enabled_systems")))
check("and it says why", bool(r.json().get("skipped_systems_reason")), str(r.json().get("skipped_systems_reason")))
with Session(database.engine) as db:
    club = db.exec(select(Club).where(Club.slug == "nonight")).first()
    check("no ClubSystem row invented a Wednesday",
          db.exec(select(ClubSystem).where(ClubSystem.club_id == club.id)).first() is None)

print("\n11c. The club night's start time reaches the club")
with Session(database.engine) as db:
    cs = db.exec(select(ClubSystem).where(ClubSystem.session_start_time == "18:00")).first()
    check("session_start_time was set from the request", cs is not None)

print("\n11d. A club can be provisioned without going public")
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-quiet2", "Sam", None))
client.post("/club-requests", json={**REQUEST, "club_name": "Held Back", "preferred_slug": "heldback"})
with Session(database.engine) as db:
    hb = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-quiet2")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{hb}/provision", json={"slug": "heldback", "active": False})
check("provision succeeded", r.status_code == 200, r.text[:140])
with Session(database.engine) as db:
    club = db.exec(select(Club).where(Club.slug == "heldback")).first()
    check("the club exists but is not active", club is not None and club.active is False)
    owner = db.exec(select(User).where(User.discord_id == "discord-quiet2")).first()
    check("its owner can still administer it", owner is not None and owner.is_super_admin)

print("\n11e. Likely duplicates are shown to the reviewer")
r = client.get("/admin/platform/club-requests")
rows = r.json() if r.status_code == 200 else []
dupes = [x for x in rows if x.get("possible_duplicates")]
check("at least one request flags an existing club", len(dupes) >= 1,
      f"{len(rows)} rows, none flagged")
if dupes:
    names = [d["name"] for d in dupes[0]["possible_duplicates"]]
    check("naming the club it might already be", len(names) >= 1, str(names))

print("\n11f. Provisioning must not leave another club without an owner")
# Club-scoped admin authority is `is_super_admin AND club_id == this club`, and
# there is no multi-club admin model, so appointing someone here MOVES them. If
# they were the only super-admin somewhere else, that club silently loses its
# owner and nobody notices until something needs changing.
club_emails.send_email = _fake_send
# Ids left to the database: provisioning creates clubs of its own between these
# blocks, and any number picked by hand gets consumed by the next autoincrement.
with Session(database.engine) as db:
    solo_club = Club(name="Solo Club", slug="solo")
    db.add(solo_club)
    db.flush()
    solo_club_id = solo_club.id
    db.add(User(discord_id="discord-solo", discord_name="Robin",
                club_id=solo_club_id, is_super_admin=True))
    db.commit()
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-solo", "Robin", None))
client.post("/club-requests", json={**REQUEST, "club_name": "Robin's Second Club",
                                    "preferred_slug": "second"})
with Session(database.engine) as db:
    second_id = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-solo")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))

r = client.post(f"/admin/platform/club-requests/{second_id}/provision", json={"slug": "second"})
check("refused with a conflict", r.status_code == 409, f"got {r.status_code}: {r.text[:120]}")
check("naming the club that would be left stranded",
      "Solo Club" in r.json().get("detail", ""), r.json().get("detail", "")[:140])
with Session(database.engine) as db:
    check("no half-created club was left behind",
          db.exec(select(Club).where(Club.slug == "second")).first() is None)
    check("the request is still pending, not half-approved",
          db.get(ClubRequest, second_id).status == "pending")
    check("Robin still runs their original club",
          db.exec(select(User).where(User.discord_id == "discord-solo")).first().club_id == solo_club_id)

print("\n11g. ...but the reviewer can still go ahead without appointing")
r = client.post(f"/admin/platform/club-requests/{second_id}/provision",
                json={"slug": "second", "appoint_super_admin": False})
check("provision succeeded", r.status_code == 200, r.text[:140])
with Session(database.engine) as db:
    check("the club exists", db.exec(select(Club).where(Club.slug == "second")).first() is not None)
    check("and Robin is untouched",
          db.exec(select(User).where(User.discord_id == "discord-solo")).first().club_id == solo_club_id)

print("\n11h. Moving is fine when the old club keeps an owner")
with Session(database.engine) as db:
    shared = Club(name="Shared Club", slug="shared")
    db.add(shared)
    db.flush()
    shared_id = shared.id
    db.add(User(discord_id="discord-pair-a", discord_name="Alex",
                club_id=shared_id, is_super_admin=True))
    db.add(User(discord_id="discord-pair-b", discord_name="Blake",
                club_id=shared_id, is_super_admin=True))
    db.commit()
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-pair-a", "Alex", None))
client.post("/club-requests", json={**REQUEST, "club_name": "Alex's Club", "preferred_slug": "alexs"})
with Session(database.engine) as db:
    alex_req = db.exec(select(ClubRequest).where(ClubRequest.discord_id == "discord-pair-a")).first().id
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.post(f"/admin/platform/club-requests/{alex_req}/provision", json={"slug": "alexs"})
check("allowed, because Blake is still there", r.status_code == 200, r.text[:140])
with Session(database.engine) as db:
    new_club = db.exec(select(Club).where(Club.slug == "alexs")).first()
    alex = db.exec(select(User).where(User.discord_id == "discord-pair-a")).first()
    check("Alex now runs the new club", alex.club_id == new_club.id and alex.is_super_admin)
    left = db.exec(select(User).where(User.club_id == shared_id, User.is_super_admin == True)).all()
    check("Shared Club still has an owner", len(left) == 1 and left[0].discord_name == "Blake",
          str([u.discord_name for u in left]))

print("\n12. The copy is editable from platform admin")
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
r = client.get("/admin/platform/club-emails")
check("all three emails are listed", r.status_code == 200 and len(r.json()) == 3, r.text[:120])
kinds = {e["kind"] for e in r.json()} if r.status_code == 200 else set()
check("by their known kinds",
      kinds == {"request_received", "club_live", "request_declined"}, str(kinds))
check("none customised to begin with", all(not e["customised"] for e in r.json()))

r = client.post("/admin/platform/club-emails", json={
    "kind": "request_received",
    "subject": "Cheers {requester_name}!",
    "body": "Hi {requester_name},\n\nWe got it — {club_name}.",
})
check("an edit saves", r.status_code == 200, r.text[:120])
check("and reads back", r.json().get("subject") == "Cheers {requester_name}!")

SENT_EMAILS.clear()
client.cookies.delete("cta_session")
client.cookies.set("cta_pending_signup", _make_pending_signup_cookie("discord-edit", "Edi", None))
client.post("/club-requests", json={**REQUEST, "club_name": "Edited Club", "preferred_slug": "edited"})
# "Nick", not the Discord handle "Edi": the email greets people by the name they
# typed on the form, which is the one they'd expect to be called.
check("the edited copy is what actually goes out",
      SENT_EMAILS and SENT_EMAILS[0]["subject"] == "Cheers Nick!",
      str([e["subject"] for e in SENT_EMAILS]))
check("and the edited body too",
      SENT_EMAILS and "We got it — Edited Club." in SENT_EMAILS[0]["html"],
      SENT_EMAILS[0]["html"][:160] if SENT_EMAILS else "")
check("sent from the onboarding address, with a display name",
      SENT_EMAILS and SENT_EMAILS[0]["from"] == "Call to Arms <noreply@calltoarms.app>",
      str(SENT_EMAILS[0]["from"]) if SENT_EMAILS else "")
check("a plain-text alternative goes with every send",
      SENT_EMAILS and "We got it — Edited Club." in (SENT_EMAILS[0]["text"] or ""),
      str(SENT_EMAILS[0]["text"])[:100] if SENT_EMAILS else "")

print("\n13. Clearing a template restores the built-in wording")
client.cookies.delete("cta_pending_signup")
client.cookies.set("cta_session", _make_session_cookie(1))
client.post("/admin/platform/club-emails", json={"kind": "request_received", "subject": "", "body": ""})
r = client.get("/admin/platform/club-emails")
row = next(e for e in r.json() if e["kind"] == "request_received")
check("back to the default", row["subject"] == row["default_subject"], row["subject"])
check("and marked as not customised", row["customised"] is False)

print("\n14. Preview renders drafts, and flags typos in tokens")
r = client.post("/admin/platform/club-emails/preview", json={
    "kind": "club_live",
    "subject": "{club_name} is ready",
    "body": "Hi {requester_name},\n\n{club_url}\n\n{club_nmae} is a typo.",
})
check("preview succeeded", r.status_code == 200, r.text[:120])
pv = r.json() if r.status_code == 200 else {}
check("subject filled with sample data", pv.get("subject") == "Badmoon Bunker is ready", str(pv.get("subject")))
check("the club address became a link", "<a href=" in pv.get("html", ""))
check("a mistyped token is reported", pv.get("unknown_tokens") == ["club_nmae"], str(pv.get("unknown_tokens")))

print("\n15. An editor cannot inject markup into an email we send")
r = client.post("/admin/platform/club-emails/preview", json={
    "kind": "request_declined",
    "subject": "x",
    "body": "Hi <script>alert(1)</script> and <b>bold</b>",
})
html = r.json().get("html", "")
check("script and tags are inert", "<script" not in html and "<b>" not in html, html[:120])
check("the text itself survives", "alert(1)" in html)

print("\n15b. The branded wrapper is actually wrapped around it")
r = client.post("/admin/platform/club-emails/preview",
                json={"kind": "club_live", "subject": "x", "body": "Hi {requester_name}"})
pv = r.json()
html = pv["html"]
check("the logo is in there", "email-logo.png" in html, html[:100])
check("laid out with tables, which is what email clients render",
      html.count("<table") >= 3, str(html.count("<table")))
check("styles are inline, not in a stripped <style> block",
      "style=" in html and html.count("<style") <= 1)
check("the club link is a button, not just a bare URL",
      "Open your club" in html and "https://badmoon.calltoarms.app" in html)
check("a plain-text alternative comes back too",
      pv.get("text", "").startswith("Hi Nick"), str(pv.get("text"))[:60])
# The wrapper must not become a way round the escaping.
r = client.post("/admin/platform/club-emails/preview",
                json={"kind": "club_live", "subject": "x", "body": "<script>alert(1)</script>"})
check("an editor still cannot inject through the wrapper",
      "<script>alert(1)" not in r.json()["html"])

print("\n16. Unknown kinds are refused everywhere")
for path in ["/admin/platform/club-emails", "/admin/platform/club-emails/preview"]:
    rr = client.post(path, json={"kind": "nonsense", "subject": "x", "body": "y"})
    check(f"422 from {path}", rr.status_code == 422, f"got {rr.status_code}")

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
