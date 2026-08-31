"""Ticketing: holds, the waitlist, and settling a payment.

The operational half of ticketing, which works with no Stripe at all. Signature
verification is tested separately in test_stripe_webhook.py.

Run: PYTHONPATH=. python tests/test_ticketing.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

DB = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB

from sqlmodel import SQLModel, Session, select   # noqa: E402
import database                                   # noqa: E402
from database import engine                       # noqa: E402
import models as M                                # noqa: E402
import tickets                                    # noqa: E402

database.WRITE_ALLOWED_TABLES = database.WRITE_ALLOWED_TABLES | {
    "clubs", "users", "players", "systems", "club_systems"}
SQLModel.metadata.create_all(engine)

FAILURES = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond: FAILURES.append(label)


def entry(db, t, name, status="registered", ticket="none", hold=None):
    e = M.TournamentEntry(tournament_id=t.id, display_name=name, status=status,
                          ticket_status=ticket, hold_expires_at=hold)
    db.add(e); db.commit(); db.refresh(e)
    return e


with Session(engine) as db:
    club = M.Club(name="C", slug="egnwgc", active=True); db.add(club); db.commit(); db.refresh(club)
    sc = M.SystemConfig(name="HH", slug="hh", legacy_system_name="HH", active=True)
    db.add(sc); db.commit(); db.refresh(sc)

    def make(**kw):
        t = M.Tournament(club_id=club.id, system_id=sc.id, name="E",
                         event_date=date(2026, 11, 14), rounds=3, status="open", **kw)
        db.add(t); db.commit(); db.refresh(t)
        return t

    print("\nA free event has no ticketing")
    free = make()
    check("a free event doesn't charge", not tickets.needs_payment(free))
    check("a free event sets no hold", tickets.hold_expiry(free) is None)

    print("\nA paid event holds a place for a while")
    paid = make(ticket_price_pence=3500, ticket_hold_hours=72, capacity=2)
    exp = tickets.hold_expiry(paid, now=datetime(2026, 1, 1, 12, 0))
    check("the hold is the configured number of hours out",
          exp == datetime(2026, 1, 4, 12, 0), str(exp))
    check("a hold of zero hours means never",
          tickets.hold_expiry(make(ticket_price_pence=3500, ticket_hold_hours=0)) is None)

    print("\nMarking paid")
    e = entry(db, paid, "Ada", hold=datetime.utcnow() + timedelta(hours=1))
    tickets.mark_paid(e, amount_pence=3500)
    db.add(e); db.commit()
    check("status becomes paid", e.ticket_status == "paid")
    check("the amount is recorded", e.amount_paid_pence == 3500)
    check("a paid place stops lapsing", e.hold_expires_at is None)
    comp = entry(db, paid, "Guest")
    tickets.mark_paid(comp, comp=True)
    check("a comp is settled but not counted as money",
          comp.ticket_status == "comp" and comp.amount_paid_pence is None)

    print("\nLapsed holds free the place, and the waitlist moves up")
    ev = make(ticket_price_pence=3500, ticket_hold_hours=1, capacity=2)
    past = datetime.utcnow() - timedelta(hours=2)
    a = entry(db, ev, "Never-paid", hold=past)
    b = entry(db, ev, "Paid", ticket="paid")
    c = entry(db, ev, "Waiting", status="waitlisted")
    res = tickets.expire_holds(db)
    db.refresh(a); db.refresh(b); db.refresh(c)
    check("the unpaid hold lapses", a.status == "waitlisted", a.status)
    check("the paid entry is untouched", b.status == "registered" and b.ticket_status == "paid")
    check("the person waiting is promoted", c.status == "registered", c.status)
    check("the promoted entry gets its own hold", c.hold_expires_at is not None)
    check("the run reports what it did",
          res["expired"] == 1 and res["promoted"] == 1, str(res))
    check("a lapsed entry is kept, not deleted — a TO can reinstate them",
          db.get(M.TournamentEntry, a.id) is not None)

    print("\nNothing lapses before its time")
    ev2 = make(ticket_price_pence=3500, ticket_hold_hours=72, capacity=4)
    fresh = entry(db, ev2, "Fresh", hold=datetime.utcnow() + timedelta(hours=48))
    tickets.expire_holds(db)
    db.refresh(fresh)
    check("a hold in the future is left alone", fresh.status == "registered")

    print("\nWaitlist promotion respects capacity and order")
    ev3 = make(ticket_price_pence=1000, capacity=2)
    p1 = entry(db, ev3, "First")
    p2 = entry(db, ev3, "Second")
    w1 = entry(db, ev3, "Wait A", status="waitlisted")
    w2 = entry(db, ev3, "Wait B", status="waitlisted")
    check("a full event promotes nobody", tickets.promote_waitlist(db, ev3) == [])
    p1.status = "dropped"; db.add(p1); db.commit()
    promoted = tickets.promote_waitlist(db, ev3); db.commit()
    check("one drop promotes exactly one",
          [p.display_name for p in promoted] == ["Wait A"], str([p.display_name for p in promoted]))
    db.refresh(w2)
    check("the rest stay waiting", w2.status == "waitlisted")

    print("\nThe TO's reconciliation view")
    s = tickets.summary(db, ev3)
    check("it counts paid and unpaid", s["paid"] + s["unpaid"] == 2, str(s))
    check("it reports what's expected", s["expected_pence"] == 2000, str(s["expected_pence"]))
    check("an uncapped event promotes nobody", tickets.promote_waitlist(db, make()) == [])

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
