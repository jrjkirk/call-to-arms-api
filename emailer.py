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


def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    cc: list[str] | None = None,
) -> str:
    """Send an email via Resend. Returns the Resend message id.

    Raises RuntimeError on a non-2xx Resend response so the caller can
    surface/log the failure cleanly."""
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
        raise RuntimeError(
            f"Resend send failed ({resp.status_code}): {resp.text[:300]}"
        )

    return resp.json().get("id", "")
