"""Resend email helper for server-initiated notifications (table-booking
alerts to venues, and any future transactional email).

Config (env, server-side only):
  RESEND_API_KEY  - Resend API key ("Sending access" scope is sufficient)
  EMAIL_FROM      - verified sender address, e.g. notifications@calltoarms.app

Read at call time (not import time), so the module imports fine in
environments where email isn't configured yet — it only raises when a send
is actually attempted.
"""
import os

import httpx


def _config() -> tuple[str, str]:
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("EMAIL_FROM", "")
    if not api_key or not from_addr:
        raise RuntimeError(
            "Email is not configured: set RESEND_API_KEY and EMAIL_FROM."
        )
    return api_key, from_addr


class UndeliverableRecipient(RuntimeError):
    """The recipient address was rejected, so this send will never work.

    Split out from a plain RuntimeError because the two failures want opposite
    handling. A bad API key or a Resend outage is an operator problem and
    should raise an alert. Someone mistyping their email address on a public
    booking form is ordinary user input, happens on any form open to the
    public, and must not page anybody -- the booking still stands, and the
    confirmation screen already tells them we couldn't reach them.
    """


def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    cc: list[str] | None = None,
) -> str:
    """Send an email via Resend. Returns the Resend message id.

    Raises UndeliverableRecipient when Resend rejects the recipient address,
    and RuntimeError on any other non-2xx response, so callers can tell
    "this person's address is wrong" from "our email is broken"."""
    api_key, from_addr = _config()

    payload = {
        "from": from_addr,
        "to": [to] if isinstance(to, str) else to,
        "subject": subject,
        "html": html,
    }
    if cc:
        payload["cc"] = cc

    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if resp.status_code >= 300:
        body = resp.text[:300]
        # Resend names the offending field in the message. Only a complaint
        # about `to` means the address is at fault -- the same 422 about `from`
        # is a misconfigured sender, which very much is an operator problem, so
        # anything we can't attribute to the recipient stays a loud failure.
        if resp.status_code == 422 and "to field" in body.lower():
            raise UndeliverableRecipient(
                f"Resend rejected the recipient ({resp.status_code}): {body}"
            )
        raise RuntimeError(
            f"Resend send failed ({resp.status_code}): {body}"
        )

    return resp.json().get("id", "")
