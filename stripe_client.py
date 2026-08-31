"""Stripe, over the raw HTTP API.

No SDK, matching how emailer.py talks to Resend and how everything else here
talks to Discord. The trade is deliberate: one fewer dependency on a 256 MB
machine, at the cost of implementing webhook signature verification ourselves —
which is the one part of this that is genuinely security-critical, so it is
written carefully and tested hard (see tests/test_stripe_webhook.py). A forged
webhook marks a ticket paid for free.

CONNECT MODEL: Standard, club as merchant of record. Every call carries a
`Stripe-Account` header naming the club's own account, so the money moves
between the player and the club and never touches ours. Nothing here creates
transfers or payouts, because we are not a marketplace and should not become
one by accident.

Config (env, server-side only):
  STRIPE_SECRET_KEY      platform secret key
  STRIPE_WEBHOOK_SECRET  signing secret for the Connect webhook endpoint
Unset means ticketing stays manual and every entry point below says so rather
than failing obscurely.
"""
import hashlib
import hmac
import json
import os
import time
from typing import Optional

import httpx

API = "https://api.stripe.com/v1"
_TIMEOUT = httpx.Timeout(20.0, connect=8.0)
# How far out of step a webhook's timestamp may be before we reject it. Stripe
# suggests five minutes; the window exists to stop a captured request being
# replayed later.
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeNotConfigured(RuntimeError):
    """Raised instead of a confusing 401 when no key is set."""


class StripeError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def _key() -> str:
    k = os.environ.get("STRIPE_SECRET_KEY", "")
    if not k:
        raise StripeNotConfigured(
            "Card payments aren't set up on this platform yet. "
            "Take payment your usual way and mark the ticket paid."
        )
    return k


def _post(path: str, data: dict, *, account: Optional[str] = None) -> dict:
    """Form-encoded POST, which is what Stripe's API takes.

    Nested keys use Stripe's bracket notation, so a caller passes
    {"line_items[0][quantity]": 1} rather than a nested dict.
    """
    headers = {"Authorization": f"Bearer {_key()}"}
    if account:
        headers["Stripe-Account"] = account
    resp = httpx.post(f"{API}{path}", data=data, headers=headers, timeout=_TIMEOUT)
    if resp.status_code >= 300:
        body = resp.text[:400]
        raise StripeError(f"Stripe {path} failed ({resp.status_code}): {body}")
    return resp.json()


def _get(path: str, *, account: Optional[str] = None) -> dict:
    headers = {"Authorization": f"Bearer {_key()}"}
    if account:
        headers["Stripe-Account"] = account
    resp = httpx.get(f"{API}{path}", headers=headers, timeout=_TIMEOUT)
    if resp.status_code >= 300:
        raise StripeError(f"Stripe {path} failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Connect onboarding
# ---------------------------------------------------------------------------

def create_connected_account(club_name: str, email: Optional[str]) -> str:
    """A Standard connected account for a club. Standard, not Express: the club
    keeps their own Stripe dashboard, their own payouts and their own disputes,
    which is the whole point of them being the merchant."""
    data = {"type": "standard", "business_profile[name]": club_name[:120]}
    if email:
        data["email"] = email
    return _post("/accounts", data)["id"]


def onboarding_link(account_id: str, refresh_url: str, return_url: str) -> str:
    """A one-time URL where the club completes Stripe's own onboarding."""
    return _post("/account_links", {
        "account": account_id,
        "refresh_url": refresh_url,
        "return_url": return_url,
        "type": "account_onboarding",
    })["url"]


def account_status(account_id: str) -> dict:
    a = _get(f"/accounts/{account_id}")
    return {
        "id": a.get("id"),
        "charges_enabled": bool(a.get("charges_enabled")),
        "payouts_enabled": bool(a.get("payouts_enabled")),
        "details_submitted": bool(a.get("details_submitted")),
    }


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def create_checkout_session(
    *,
    account_id: str,
    amount_pence: int,
    product_name: str,
    success_url: str,
    cancel_url: str,
    entry_id: int,
    tournament_id: int,
    customer_email: Optional[str] = None,
    application_fee_pence: int = 0,
) -> dict:
    """A Checkout Session on the CLUB's account.

    entry_id and tournament_id ride along as metadata so the webhook can find
    the entry without trusting anything in the redirect — a success_url is just
    a URL a browser was told to visit, and must never be what marks a ticket
    paid.
    """
    data = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "line_items[0][price_data][currency]": "gbp",
        "line_items[0][price_data][unit_amount]": str(int(amount_pence)),
        "line_items[0][price_data][product_data][name]": product_name[:250],
        "line_items[0][quantity]": "1",
        "metadata[entry_id]": str(entry_id),
        "metadata[tournament_id]": str(tournament_id),
        "payment_intent_data[metadata][entry_id]": str(entry_id),
    }
    if customer_email:
        data["customer_email"] = customer_email
    if application_fee_pence > 0:
        data["payment_intent_data[application_fee_amount]"] = str(int(application_fee_pence))
    s = _post("/checkout/sessions", data, account=account_id)
    return {"id": s["id"], "url": s["url"]}


def get_checkout_session(session_id: str, account_id: str) -> dict:
    return _get(f"/checkout/sessions/{session_id}", account=account_id)


def refund(payment_intent: str, account_id: str) -> dict:
    return _post("/refunds", {"payment_intent": payment_intent}, account=account_id)


# ---------------------------------------------------------------------------
# Webhook signatures
# ---------------------------------------------------------------------------

def verify_webhook(payload: bytes, sig_header: str, secret: str,
                   *, now: Optional[int] = None) -> dict:
    """Verify a Stripe webhook and return the parsed event.

    The header looks like `t=1614556800,v1=abc...,v1=def...`. The signed
    message is `{timestamp}.{raw body}`, HMAC-SHA256 with the endpoint secret.

    Three things this must get right, and the reason each is here:
      - compare in constant time, so the signature can't be discovered by
        timing the comparison;
      - check the timestamp is recent, so a captured request can't be replayed
        indefinitely;
      - verify against the RAW body, never a re-serialised dict, because
        re-encoding changes the bytes and the signature is over bytes.
    """
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    if not sig_header:
        raise StripeError("Missing Stripe-Signature header.")

    timestamp = None
    signatures = []
    for part in sig_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise StripeError("Malformed Stripe-Signature header.")

    try:
        ts = int(timestamp)
    except ValueError:
        raise StripeError("Malformed timestamp in Stripe-Signature.")

    current = int(time.time()) if now is None else now
    if abs(current - ts) > WEBHOOK_TOLERANCE_SECONDS:
        raise StripeError("Stripe webhook timestamp is outside the tolerance window.")

    expected = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # Any one of the v1 signatures matching is enough — Stripe sends several
    # while a secret is being rotated.
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise StripeError("Stripe webhook signature did not match.")

    return json.loads(payload.decode())
