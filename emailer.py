"""Resend email helper for server-initiated notifications (table-booking
alerts to venues, and any future transactional email).

Config (env, server-side only):
  RESEND_API_KEY  - Resend API key ("Sending access" scope is sufficient)
  EMAIL_FROM      - verified sender address, e.g. notifications@calltoarms.app
  EMAIL_FROM_NAME - display name, default "Call to Arms"
  EMAIL_FROM_ONBOARDING / EMAIL_FROM_VENUE
                  - optional per-purpose overrides; see sender()

Resend verifies a DOMAIN, not individual addresses, so any local part on the
verified domain sends without further setup. That is what lets the purposes
below have their own addresses for free.

Read at call time (not import time), so the module imports fine in
environments where email isn't configured yet — it only raises when a send
is actually attempted.
"""
import html as _html
import os

import httpx


def esc(value) -> str:
    """Escape a value for interpolation into HTML we build ourselves.

    Every HTML body in this codebase is assembled with f-strings, and the
    values going into them are not ours: a booker's name, phone and notes come
    straight off a public form that needs no account, and a player's name is
    whatever they typed at signup. None of it was escaped.

    That mattered most for the table-booking email, because
    `admin/table-booking/preview` hands the same HTML back to the admin page,
    which renders it with `{@html}` — so a player could put markup in their
    name and have it execute in a club admin's browser. In the emails
    themselves the risk is milder (clients strip scripts) but a name closing a
    tag early still wrecks the layout, and a crafted link is a phishing vector
    pointed at venue staff.

    `None` renders as an empty string rather than the word "None", which is
    what a caller interpolating an optional field wants anyway.
    """
    return "" if value is None else _html.escape(str(value), quote=True)


def _config() -> tuple[str, str]:
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("EMAIL_FROM", "")
    if not api_key or not from_addr:
        raise RuntimeError(
            "Email is not configured: set RESEND_API_KEY and EMAIL_FROM."
        )
    return api_key, from_addr


# Which address a given kind of mail comes from. Both default to the local part
# the purpose deserves, on whatever domain EMAIL_FROM already uses:
#
#   onboarding -> noreply@   nothing here can be replied to (the domain has no
#                            MX records), and saying so in the address is more
#                            honest than a friendly one that bounces
#   venue      -> notifications@  a venue getting a booking alert is being
#                            notified about something, which is what it says
#
# Overridable per purpose if either should differ.
_PURPOSE_LOCAL_PARTS = {
    "onboarding": "noreply",
    "venue": "notifications",
}

DEFAULT_FROM_NAME = "Call to Arms"


def sender(purpose: str | None = None) -> str:
    """The From header for one kind of mail, display name included.

    The display name is the part that actually reads as professional: without
    it every inbox shows the raw address, so a club organiser sees
    "notifications@calltoarms.app" where they should see "Call to Arms".

    An explicit EMAIL_FROM_<PURPOSE> wins. Otherwise the purpose's local part is
    swapped onto EMAIL_FROM's domain, so a deployment on another domain gets the
    right addresses without configuring each one.
    """
    base = os.environ.get("EMAIL_FROM", "")
    name = os.environ.get("EMAIL_FROM_NAME", DEFAULT_FROM_NAME).strip()

    addr = ""
    if purpose:
        addr = os.environ.get(f"EMAIL_FROM_{purpose.upper()}", "").strip()
    if not addr:
        local = _PURPOSE_LOCAL_PARTS.get(purpose or "")
        if local and "@" in base:
            addr = f"{local}@{base.split('@', 1)[1]}"
        else:
            addr = base

    # An address configured WITH a display name is left exactly as given:
    # someone who set "Badmoon <x@y>" meant it.
    if not addr or "<" in addr:
        return addr
    return f"{name} <{addr}>" if name else addr


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
    text: str | None = None,
    from_addr: str | None = None,
) -> str:
    """Send an email via Resend. Returns the Resend message id.

    Raises UndeliverableRecipient when Resend rejects the recipient address,
    and RuntimeError on any other non-2xx response, so callers can tell
    "this person's address is wrong" from "our email is broken"."""
    api_key, default_from = _config()

    payload = {
        "from": from_addr or default_from,
        "to": [to] if isinstance(to, str) else to,
        "subject": subject,
        "html": html,
    }
    # A plain-text alternative alongside the HTML. Worth sending: spam filters
    # mark down HTML-only mail, and a text part is what a screen reader or a
    # plain-text client actually reads instead of falling back to tag soup.
    if text:
        payload["text"] = text
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
