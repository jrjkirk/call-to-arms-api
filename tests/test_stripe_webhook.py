"""Stripe webhook signature verification.

This is the one piece of the ticketing feature where getting it wrong has a
direct cost: a forged webhook marks a ticket paid for free. It is hand-rolled
rather than taken from the SDK (see stripe_client's docstring), so it is tested
against every way it could be got wrong.

Run: PYTHONPATH=. python tests/test_stripe_webhook.py
"""
import hashlib
import hmac
import json
import sys
import time

import stripe_client as sc

SECRET = "whsec_test_secret"
FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


def sign(payload: bytes, secret=SECRET, ts=None, scheme="v1"):
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},{scheme}={mac}"


def rejects(payload, header, secret=SECRET, now=None):
    try:
        sc.verify_webhook(payload, header, secret, now=now)
        return False
    except (sc.StripeError, sc.StripeNotConfigured):
        return True


body = json.dumps({"id": "evt_1", "type": "checkout.session.completed"}).encode()

print("\nAccepting a genuine event")
ev = sc.verify_webhook(body, sign(body), SECRET)
check("a correctly signed event is accepted", ev["id"] == "evt_1")
check("the parsed event comes back", ev["type"] == "checkout.session.completed")

print("\nRejecting everything else")
check("a tampered body is rejected",
      rejects(body.replace(b"evt_1", b"evt_2"), sign(body)))
check("a signature made with the wrong secret is rejected",
      rejects(body, sign(body, secret="whsec_attacker")))
check("a missing header is rejected", rejects(body, ""))
check("a header with no signature is rejected", rejects(body, "t=123"))
check("a header with no timestamp is rejected", rejects(body, "v1=abc"))
check("a non-numeric timestamp is rejected", rejects(body, "t=notanumber,v1=abc"))
check("gibberish is rejected", rejects(body, "nonsense"))
check("an unset endpoint secret is rejected, not skipped",
      rejects(body, sign(body), secret=""))

print("\nReplay")
old = int(time.time()) - (sc.WEBHOOK_TOLERANCE_SECONDS + 60)
check("a signature older than the tolerance window is rejected",
      rejects(body, sign(body, ts=old)))
future = int(time.time()) + (sc.WEBHOOK_TOLERANCE_SECONDS + 60)
check("a far-future timestamp is rejected", rejects(body, sign(body, ts=future)))
recent = int(time.time()) - 30
check("a signature from 30 seconds ago is still accepted",
      not rejects(body, sign(body, ts=recent)))

print("\nSecret rotation")
ts = int(time.time())
good = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
bad = "0" * 64
check("one matching v1 among several is enough",
      sc.verify_webhook(body, f"t={ts},v1={bad},v1={good}", SECRET)["id"] == "evt_1")
check("all-wrong signatures are still rejected",
      rejects(body, f"t={ts},v1={bad},v1={bad}"))

print("\nBytes, not objects")
# Re-serialising JSON changes whitespace and therefore the bytes. Verifying
# against anything but the raw body would break on a payload Stripe formats
# differently to json.dumps.
spaced = b'{ "id" : "evt_1" }'
check("verification is over the exact bytes received",
      sc.verify_webhook(spaced, sign(spaced), SECRET)["id"] == "evt_1")
check("the same content re-serialised does NOT verify",
      rejects(json.dumps(json.loads(spaced)).encode(), sign(spaced)))

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
