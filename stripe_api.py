"""Stripe endpoints: the club's own Connect onboarding, and the webhook.

Separate from tournaments.py because none of this is tournament-specific — a
club connects Stripe once, and the webhook is a single endpoint Stripe posts
to for every club on the platform.
"""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

import stripe_client
import tickets
from auth import active_club_id, require_user
from database import club_app_url, get_session
from models import Club, Tournament, TournamentEntry, User
from observability import capture

router = APIRouter(prefix="/stripe", tags=["stripe"])


def _require_owner(db: Session, user: User, club_id: int) -> Club:
    """Connecting a bank account is an ownership decision, not a day-to-day
    one — the same bar as handing out venue keys."""
    if not (user.is_platform_admin or (user.is_super_admin and user.club_id == club_id)):
        raise HTTPException(status_code=403, detail="Only a club super-admin can do that.")
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")
    return club


@router.get("/status")
def status(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Whether this club can take card payments, and how far through setup it
    is. Reads the mirrored flag first so a page load costs no API call."""
    club = _require_owner(db, user, club_id)
    out = {
        "platform_configured": stripe_client.configured(),
        "connected": bool(club.stripe_account_id),
        "charges_enabled": club.stripe_charges_enabled,
        "account_id": club.stripe_account_id,
    }
    if club.stripe_account_id and stripe_client.configured():
        try:
            live = stripe_client.account_status(club.stripe_account_id)
            out.update(live)
            # Keep the mirror honest whenever we've asked Stripe anyway.
            if club.stripe_charges_enabled != live["charges_enabled"]:
                club.stripe_charges_enabled = live["charges_enabled"]
                db.add(club)
                db.commit()
        except stripe_client.StripeError as e:
            capture(e, kind="stripe_status", club_id=club_id)
            out["error"] = "Couldn't reach Stripe just now."
    return out


@router.post("/connect")
def connect(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Start or resume Stripe onboarding. Returns a one-time URL to send the
    club owner to; Stripe brings them back to Venue Admin when they're done."""
    club = _require_owner(db, user, club_id)
    if not stripe_client.configured():
        raise HTTPException(
            status_code=409,
            detail="Card payments aren't set up on this platform yet.",
        )
    try:
        if not club.stripe_account_id:
            club.stripe_account_id = stripe_client.create_connected_account(
                club.name, getattr(club, "contact_email", None)
            )
            db.add(club)
            db.commit()
        base = club_app_url(club)
        url = stripe_client.onboarding_link(
            club.stripe_account_id,
            refresh_url=f"{base}/venue-admin?stripe=refresh",
            return_url=f"{base}/venue-admin?stripe=done",
        )
    except stripe_client.StripeError as e:
        capture(e, kind="stripe_connect", club_id=club_id)
        raise HTTPException(status_code=502, detail="Stripe wouldn't start that setup.")
    return {"ok": True, "onboarding_url": url}


@router.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_session)):
    """Stripe's callback. THE only thing that marks a ticket paid.

    Deliberately not the success_url: that is a URL a browser was told to
    visit, and anybody can visit it. Payment is confirmed here, against a
    signature, or not at all.

    Always 200s once the signature checks out, even for events we ignore —
    Stripe retries anything else, and retrying an event we understood and chose
    not to act on achieves nothing.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    payload = await request.body()
    try:
        event = stripe_client.verify_webhook(
            payload, request.headers.get("stripe-signature", ""), secret
        )
    except stripe_client.StripeNotConfigured:
        raise HTTPException(status_code=503, detail="Webhooks are not configured.")
    except stripe_client.StripeError as e:
        # A bad signature is a 400, never a 500: it is a rejected request, not
        # a broken one, and Stripe should not retry it.
        raise HTTPException(status_code=400, detail=str(e))

    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        _settle(db, obj)
    elif kind in ("charge.refunded", "charge.refund.updated"):
        _unsettle(db, obj)
    elif kind == "account.updated":
        _sync_account(db, obj)

    return {"received": True}


def _entry_from_metadata(db: Session, obj: dict) -> Optional[TournamentEntry]:
    meta = obj.get("metadata") or {}
    raw = meta.get("entry_id")
    if not raw:
        return None
    try:
        return db.get(TournamentEntry, int(raw))
    except (TypeError, ValueError):
        return None


def _settle(db: Session, session: dict) -> None:
    if session.get("payment_status") != "paid":
        return
    entry = _entry_from_metadata(db, session)
    if entry is None:
        # Fall back to the session id we stored when checkout started, in case
        # metadata was stripped somewhere along the way.
        sid = session.get("id")
        entry = db.exec(
            select(TournamentEntry).where(TournamentEntry.stripe_session_id == sid)
        ).first() if sid else None
    if entry is None or entry.ticket_status in tickets.PAID_STATUSES:
        return          # unknown, or already settled — webhooks arrive twice

    tickets.mark_paid(
        entry,
        amount_pence=session.get("amount_total"),
        payment_intent=session.get("payment_intent"),
    )
    db.add(entry)
    db.commit()


def _unsettle(db: Session, charge: dict) -> None:
    """A refund issued from the Stripe dashboard should free the place here
    too, or the club's two systems disagree about who is coming."""
    intent = charge.get("payment_intent")
    if not intent:
        return
    entry = db.exec(
        select(TournamentEntry).where(TournamentEntry.stripe_payment_intent == intent)
    ).first()
    if entry is None or entry.ticket_status == "refunded":
        return
    entry.ticket_status = "refunded"
    entry.status = "dropped"
    entry.hold_expires_at = None
    entry.updated_at = datetime.utcnow()
    db.add(entry)
    db.commit()

    t = db.get(Tournament, entry.tournament_id)
    if t:
        tickets.promote_waitlist(db, t)
        db.commit()


def _sync_account(db: Session, account: dict) -> None:
    acct = account.get("id")
    if not acct:
        return
    club = db.exec(select(Club).where(Club.stripe_account_id == acct)).first()
    if club is None:
        return
    club.stripe_charges_enabled = bool(account.get("charges_enabled"))
    db.add(club)
    db.commit()
