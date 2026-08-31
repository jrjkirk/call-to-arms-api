"""Ticketing for tournament entries.

Two paths, and the manual one is not a fallback — it is how most clubs will run
for a long time. A TO takes money however they already do and marks the ticket
paid; check-in reads that. Stripe, when a club connects it, automates the
marking and nothing else.

The club is the merchant of record throughout (see stripe_client), so nothing
here moves money between accounts, holds a balance, or issues a payout.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from models import Club, Tournament, TournamentEntry

# Statuses that occupy a place in the event.
HOLDING_STATUSES = ("registered", "checked_in")
PAID_STATUSES = ("paid", "comp")


def needs_payment(t: Tournament) -> bool:
    """Whether this event charges at all. A free club event has no ticketing to
    manage, and shouldn't grow a payments screen it will never use."""
    return bool(t.ticket_price_pence and t.ticket_price_pence > 0)


def hold_expiry(t: Tournament, now: Optional[datetime] = None) -> Optional[datetime]:
    """When an unpaid entry created now would lapse. None means never."""
    if not needs_payment(t) or not t.ticket_hold_hours:
        return None
    return (now or datetime.utcnow()) + timedelta(hours=t.ticket_hold_hours)


def mark_paid(entry: TournamentEntry, *, amount_pence: Optional[int] = None,
              payment_intent: Optional[str] = None, comp: bool = False) -> None:
    """Record a ticket as settled, however it was settled. The hold is cleared
    because a paid place never lapses."""
    entry.ticket_status = "comp" if comp else "paid"
    entry.paid_at = datetime.utcnow()
    entry.hold_expires_at = None
    if amount_pence is not None:
        entry.amount_paid_pence = amount_pence
    if payment_intent:
        entry.stripe_payment_intent = payment_intent
    entry.updated_at = datetime.utcnow()


def summary(db: Session, t: Tournament) -> dict:
    """What a TO needs to reconcile: who has paid, who hasn't, and how much has
    come in."""
    entries = db.exec(
        select(TournamentEntry).where(TournamentEntry.tournament_id == t.id)
    ).all()
    live = [e for e in entries if e.status in HOLDING_STATUSES]
    paid = [e for e in live if e.ticket_status in PAID_STATUSES]
    unpaid = [e for e in live if e.ticket_status not in PAID_STATUSES]
    now = datetime.utcnow()
    return {
        "charges": needs_payment(t),
        "price_pence": t.ticket_price_pence,
        "hold_hours": t.ticket_hold_hours,
        "paid": len(paid),
        "unpaid": len(unpaid),
        "waitlisted": len([e for e in entries if e.status == "waitlisted"]),
        "taken_pence": sum(e.amount_paid_pence or 0 for e in paid),
        # Expected, not taken: comps are counted as places filled but not money.
        "expected_pence": (t.ticket_price_pence or 0) * len(live),
        "lapsing_soon": [
            {"entry_id": e.id, "name": e.display_name,
             "expires": e.hold_expires_at.isoformat()}
            for e in unpaid
            if e.hold_expires_at and e.hold_expires_at <= now + timedelta(hours=24)
        ],
    }


def promote_waitlist(db: Session, t: Tournament) -> list[TournamentEntry]:
    """Move people off the waiting list into any places that have opened up.

    Called whenever a place frees: a drop, a refund, or a lapsed hold. Without
    this, a waitlist is a list of people who never find out they got in.
    """
    if not t.capacity:
        return []
    entries = db.exec(
        select(TournamentEntry)
        .where(TournamentEntry.tournament_id == t.id)
        .order_by(TournamentEntry.id)
    ).all()
    live = [e for e in entries if e.status in HOLDING_STATUSES]
    spare = t.capacity - len(live)
    if spare <= 0:
        return []

    # First onto the waiting list, first off it — the only order anybody
    # accepts as fair. Ordered by when they joined it, NOT by entry id: an
    # entry demoted for not paying rejoins the list now, and must not jump
    # ahead of people who have been waiting since they registered.
    waiting = sorted(
        (e for e in entries if e.status == "waitlisted"),
        key=lambda e: (e.waitlisted_at or datetime.min, e.id),
    )
    promoted = []
    for e in waiting[:spare]:
        e.status = "registered"
        e.waitlisted_at = None
        e.hold_expires_at = hold_expiry(t)
        e.updated_at = datetime.utcnow()
        db.add(e)
        promoted.append(e)
    return promoted


def expire_holds(db: Session, now: Optional[datetime] = None) -> dict:
    """Release unpaid places whose hold has lapsed, across every event.

    Run from the scheduler. Deliberately does NOT delete the entry: the person
    still wanted to come, and a TO who takes their money late should be able to
    put them back with one click rather than re-typing them.
    """
    now = now or datetime.utcnow()
    lapsed = db.exec(
        select(TournamentEntry)
        .where(TournamentEntry.hold_expires_at != None)  # noqa: E711
        .where(TournamentEntry.hold_expires_at <= now)
        .where(TournamentEntry.status.in_(HOLDING_STATUSES))
        .where(TournamentEntry.ticket_status.notin_(PAID_STATUSES))
    ).all()

    touched: set[int] = set()
    for e in lapsed:
        e.status = "waitlisted"
        # Stamped NOW, so they go to the back of the queue. They had their
        # place and did not pay for it; the people already waiting come first.
        e.waitlisted_at = now
        e.hold_expires_at = None
        e.updated_at = now
        db.add(e)
        touched.add(e.tournament_id)
    # Flushed before promotion so the pass below sees the demotions it must
    # take account of — otherwise it fills places that are still marked taken.
    if lapsed:
        db.flush()

    promoted = 0
    for tid in touched:
        t = db.get(Tournament, tid)
        if t and t.status in ("open", "closed"):
            promoted += len(promote_waitlist(db, t))

    if lapsed:
        db.commit()
    return {"expired": len(lapsed), "promoted": promoted}


def club_can_take_cards(db: Session, club_id: int) -> bool:
    club = db.get(Club, club_id)
    return bool(club and club.stripe_account_id and club.stripe_charges_enabled)
