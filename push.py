"""Push notifications: the in-app notices, sent to a player's phone as well.

Every notice notices.record() writes (pairings up, opponent dropped, a late
signup found you a game, a faction request decided) also goes out as a Web
Push to each device that account has allowed, unless that device has muted
that kind. Nothing here decides WHEN to notify: that stays in notices.py, so a
banner and a buzz can never disagree about what happened.

Sent after the transaction commits, never before. record() runs inside the
caller's transaction (a publish, a drop-out); queueing the push on the session
and sending it from an after_commit hook means a publish that fails and rolls
back can't have already buzzed fourteen phones. Sending happens on a thread so
a slow push service can't hold up the request that caused it.

Off until VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are set (Fly secrets; see
scripts/generate_vapid_keys.py). Without them the queue is simply dropped and
GET /push/key tells the app push isn't available, so nothing offers it.

iPhones only get push once the app has been added to the home screen and opened
from there (iOS 16.4+). That's Apple's rule, not something the server can help.
"""
import json
import logging
import os
import threading
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import event
from sqlalchemy.orm import Session as OrmSession
from sqlmodel import Session, select

from auth import require_user
from database import engine, get_session
from models import PushSubscription, User

router = APIRouter(prefix="/push", tags=["push"])
log = logging.getLogger(__name__)

VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:hello@calltoarms.app")

# What a device can switch off, in the order the Account page lists them. The
# kinds are notices.py's; the labels are what a player is choosing between.
#
# Only these are pushed: a kind with no switch here (a faction request's
# outcome, which goes to an admin) stays an in-app notice, since a device
# couldn't turn it off.
KINDS = [
    ("pairings_published", "Pairings are up"),
    ("opponent_dropped", "Opponent dropped"),
    ("game_found", "Change to pairings"),
    ("pickup", "Pick-up games"),
]

TITLES = {
    "pairings_published": "Pairings are up",
    "opponent_dropped": "Your game has changed",
    "game_found": "You've got a game",
    "faction_request": "Faction request",
    "pickup": "Pick-up game",
    "test": "Call to Arms",
}

# Tests turn this off to send inline, so they can check what was sent.
SEND_IN_BACKGROUND = True


def _keys() -> tuple[Optional[str], Optional[str]]:
    # Read at call time, not import time, so a test (or a secret set without a
    # restart in dev) is picked up.
    return os.environ.get("VAPID_PUBLIC_KEY"), os.environ.get("VAPID_PRIVATE_KEY")


def enabled() -> bool:
    public, private = _keys()
    return bool(public and private)


# ---------------------------------------------------------------------------
# Queue on the session, send after commit
# ---------------------------------------------------------------------------

def queue(db: Session, *, user_id: int, kind: str, body: str, url: str = "/", tag: Optional[str] = None) -> None:
    """Send this to the account's devices once `db`'s transaction commits."""
    if not enabled() or (kind != "test" and kind not in {k for k, _ in KINDS}):
        return
    db.info.setdefault("pending_push", []).append({
        "user_id": user_id,
        "kind": kind,
        "payload": {
            "title": TITLES.get(kind, "Call to Arms"),
            "body": body,
            "url": url,
            "tag": tag or kind,
        },
    })


@event.listens_for(OrmSession, "after_commit")
def _after_commit(session) -> None:
    pending = session.info.pop("pending_push", None)
    if not pending:
        return
    if SEND_IN_BACKGROUND:
        threading.Thread(target=send_now, args=(pending,), daemon=True).start()
    else:
        send_now(pending)


@event.listens_for(OrmSession, "after_rollback")
def _after_rollback(session) -> None:
    session.info.pop("pending_push", None)


def _send_one(sub: PushSubscription, payload: dict) -> Optional[int]:
    """Deliver one push. Returns the HTTP status on failure, None on success.
    Split out so tests can replace it without a real push service."""
    from pywebpush import WebPushException, webpush

    _, private = _keys()
    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
            data=json.dumps(payload),
            vapid_private_key=private,
            vapid_claims={"sub": VAPID_SUBJECT},
            # A pairings notice a day late is still worth having; a week late
            # is not. The push service drops it after this.
            ttl=60 * 60 * 24,
        )
        return None
    except WebPushException as e:
        return getattr(e.response, "status_code", None) or 0
    except Exception:  # network trouble: never let one device stop the rest
        log.exception("push send failed")
        return 0


def send_now(items: list[dict]) -> int:
    """Send queued pushes. Returns how many were delivered. Its own session:
    it runs after the caller's has committed, possibly on another thread."""
    delivered = 0
    with Session(engine) as db:
        for item in items:
            subs = db.exec(
                select(PushSubscription).where(PushSubscription.user_id == item["user_id"])
            ).all()
            for sub in subs:
                if item["kind"] in (sub.muted or []):
                    continue
                status = _send_one(sub, item["payload"])
                if status is None:
                    sub.last_sent_at = datetime.utcnow()
                    db.add(sub)
                    delivered += 1
                elif status in (404, 410):
                    # The browser unsubscribed or the app was removed: the
                    # address is gone for good, so is the row.
                    db.delete(sub)
        db.commit()
    return delivered


# ---------------------------------------------------------------------------
# Endpoints (the Account page's Notifications panel)
# ---------------------------------------------------------------------------

class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeBody(BaseModel):
    endpoint: str
    keys: SubscriptionKeys


class EndpointBody(BaseModel):
    endpoint: str


class MuteBody(BaseModel):
    endpoint: str
    muted: list[str]


def _own(db: Session, user: User, endpoint: str) -> Optional[PushSubscription]:
    row = db.exec(select(PushSubscription).where(PushSubscription.endpoint == endpoint)).first()
    return row if row is not None and row.user_id == user.id else None


@router.get("/key")
def push_key():
    """Public: whether push is on, the key a browser subscribes with, and the
    kinds a device can mute."""
    public, _ = _keys()
    return {
        "enabled": enabled(),
        "public_key": public if enabled() else None,
        "kinds": [{"kind": k, "label": label} for k, label in KINDS],
    }


@router.post("/subscribe")
def subscribe(
    body: SubscribeBody,
    user: User = Depends(require_user),
    user_agent: Optional[str] = Header(default=None),
    db: Session = Depends(get_session),
):
    if not enabled():
        raise HTTPException(status_code=503, detail="Notifications aren't switched on yet.")
    if not body.endpoint.startswith("https://"):
        raise HTTPException(status_code=422, detail="That isn't a push address.")
    row = db.exec(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint)).first()
    if row is None:
        row = PushSubscription(user_id=user.id, endpoint=body.endpoint, p256dh=body.keys.p256dh,
                               auth=body.keys.auth, muted=[], user_agent=(user_agent or "")[:300] or None)
    else:
        # The same browser, now signed into this account (or new keys after the
        # browser rotated them): the device follows whoever is using it.
        row.user_id = user.id
        row.p256dh = body.keys.p256dh
        row.auth = body.keys.auth
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"subscribed": True, "muted": row.muted or []}


@router.post("/unsubscribe")
def unsubscribe(body: EndpointBody, user: User = Depends(require_user), db: Session = Depends(get_session)):
    row = _own(db, user, body.endpoint)
    if row is not None:
        db.delete(row)
        db.commit()
    return {"subscribed": False}


@router.post("/status")
def status(body: EndpointBody, user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Whether THIS device is subscribed for this account. POST so the push
    address stays out of logs and URLs."""
    row = _own(db, user, body.endpoint)
    return {"subscribed": row is not None, "muted": (row.muted or []) if row else []}


@router.post("/mute")
def mute(body: MuteBody, user: User = Depends(require_user), db: Session = Depends(get_session)):
    row = _own(db, user, body.endpoint)
    if row is None:
        raise HTTPException(status_code=404, detail="This device isn't subscribed.")
    known = {k for k, _ in KINDS}
    row.muted = sorted(set(body.muted) & known)
    db.add(row)
    db.commit()
    return {"subscribed": True, "muted": row.muted}


@router.post("/test")
def test_push(user: User = Depends(require_user), db: Session = Depends(get_session)):
    """Send a test to every device on this account, now, and say how many got it."""
    if not enabled():
        raise HTTPException(status_code=503, detail="Notifications aren't switched on yet.")
    sent = send_now([{
        "user_id": user.id,
        "kind": "test",
        "payload": {"title": TITLES["test"], "body": "Notifications are working on this device.",
                    "url": "/", "tag": "test"},
    }])
    return {"delivered": sent}
