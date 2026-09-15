"""Sign in, or add an address to an account, with an emailed link
(account overhaul Slab 5).

Security shape, in one place:

* The token is 32 random bytes, sent once in the email and stored only as a
  SHA-256 hash (models.LoginToken). Single use, 15 minutes.
* The emailed link opens a page with a Continue button, and only that button's
  POST uses the token. Mail scanners (Outlook Safe Links and friends) open every
  link in a message; if opening the link signed you in, the scanner would spend
  the token before the person ever saw it.
* Rate limited per address and per IP, counted from login_tokens itself, so
  there's no second store to keep and limits hold across restarts. The IP comes
  from Fly's Fly-Client-IP header: request.client is Fly's proxy, the same
  address for everyone.
* Starting a sign-in never says whether an address has an account. The same
  answer comes back either way.
* A "link" token (adding an address from /account) completes only in a browser
  signed in to the account that asked. Otherwise someone could get a victim to
  confirm the victim's address onto the attacker's account.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

import email_layout
from emailer import send_email, sender
from models import LoginToken

TOKEN_LIFETIME = timedelta(minutes=15)
PER_EMAIL_LIMIT = 5      # links per address per hour
PER_IP_LIMIT = 20        # links per IP per hour
WINDOW = timedelta(hours=1)

SIGN_IN = "sign_in"
LINK = "link"

# Catches a typo or a pasted sentence. Deliberately loose: the email arriving is
# what proves an address works.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RateLimited(Exception):
    pass


class InvalidToken(Exception):
    pass


def normalise_email(raw: Optional[str]) -> Optional[str]:
    """Lowercased and trimmed, or None if it can't be an email address."""
    if not raw:
        return None
    email = raw.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return None
    return email


def client_ip(request) -> str:
    return (request.headers.get("fly-client-ip")
            or (request.client.host if request.client else "")
            or "unknown")


def _ip_hash(ip: str) -> str:
    key = os.environ.get("SESSION_SECRET", "").encode()
    return hmac.new(key, f"ip:{ip}".encode(), hashlib.sha256).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(db: Session, *, email: str, purpose: str, origin: str, ip: str,
          next_path: Optional[str] = None, user_id: Optional[int] = None) -> str:
    """Record a new link and return the raw token for the email. Caller commits.
    Raises RateLimited, having written nothing."""
    now = datetime.utcnow()
    since = now - WINDOW
    ip_h = _ip_hash(ip)
    recent_email = db.exec(
        select(LoginToken).where(LoginToken.email == email).where(LoginToken.created_at > since)
    ).all()
    recent_ip = db.exec(
        select(LoginToken).where(LoginToken.ip_hash == ip_h).where(LoginToken.created_at > since)
    ).all()
    if len(recent_email) >= PER_EMAIL_LIMIT or len(recent_ip) >= PER_IP_LIMIT:
        raise RateLimited()
    token = secrets.token_urlsafe(32)
    db.add(LoginToken(
        token_hash=_token_hash(token), email=email, purpose=purpose, user_id=user_id,
        origin=origin, next_path=next_path, ip_hash=ip_h,
        created_at=now, expires_at=now + TOKEN_LIFETIME,
    ))
    return token


def peek(db: Session, token: Optional[str]) -> LoginToken:
    """The live token row, without spending it. Raises InvalidToken."""
    if not token:
        raise InvalidToken()
    row = db.exec(select(LoginToken).where(LoginToken.token_hash == _token_hash(token))).first()
    if row is None or row.used_at is not None or row.expires_at <= datetime.utcnow():
        raise InvalidToken()
    return row


def spend(db: Session, row: LoginToken) -> None:
    """Mark a token used. Caller commits."""
    row.used_at = datetime.utcnow()
    db.add(row)


def link_url(origin: str, token: str) -> str:
    return f"{origin}/signin/email?token={token}"


def send_link(email: str, purpose: str, url: str) -> None:
    """Send the email. Raises what emailer.send_email raises."""
    if purpose == LINK:
        subject = "Confirm your email for Call to Arms"
        intro = "Confirm this address to add it to your Call to Arms account. You'll be able to sign in with it."
        label = "Confirm email"
        ignore = "If you didn't ask to add this address, ignore this email. Nothing changes without the link."
    else:
        subject = "Your Call to Arms sign-in link"
        intro = "Here's your link to sign in to Call to Arms."
        label = "Sign in"
        ignore = "If you didn't ask for this, ignore this email. Nobody can sign in without the link."
    lifetime = "It works once, for the next 15 minutes."
    body = email_layout.paragraphs(f"{intro}\n\n{lifetime}\n\n{ignore}")
    html = email_layout.wrap(body, preheader=intro, cta_url=url, cta_label=label)
    text = f"{intro}\n\n{label}: {url}\n\n{lifetime}\n\n{ignore}\n"
    send_email(email, subject, html, text=text, from_addr=sender("onboarding"))

