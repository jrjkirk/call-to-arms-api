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
# Signing up with a password: the hash waits on the token row until the address
# is confirmed, so an account only ever exists for an address its owner can read.
PASSWORD_SIGNUP = "password_signup"
PASSWORD_RESET = "password_reset"

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
          next_path: Optional[str] = None, user_id: Optional[int] = None,
          secret: Optional[str] = None) -> str:
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
    if purpose == PASSWORD_SIGNUP:
        # Only the newest attempt's password can be the one that lands.
        for old in db.exec(select(LoginToken).where(LoginToken.email == email)
                           .where(LoginToken.purpose == PASSWORD_SIGNUP)
                           .where(LoginToken.secret.is_not(None))).all():
            old.secret = None
            db.add(old)
    db.add(LoginToken(
        token_hash=_token_hash(token), email=email, purpose=purpose, user_id=user_id,
        origin=origin, next_path=next_path, ip_hash=ip_h,
        created_at=now, expires_at=now + TOKEN_LIFETIME, secret=secret,
    ))
    return token


def claim_signup_secret(db: Session, email: str) -> Optional[str]:
    """The password hash kept for a confirmed sign-up, taken off the row so it
    can only be used once. Caller commits."""
    row = db.exec(
        select(LoginToken).where(LoginToken.email == email)
        .where(LoginToken.purpose == PASSWORD_SIGNUP)
        .where(LoginToken.secret.is_not(None))
        .order_by(LoginToken.created_at.desc())
    ).first()
    if row is None:
        return None
    secret = row.secret
    row.secret = None
    db.add(row)
    return secret


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


def reset_url(origin: str, token: str) -> str:
    return f"{origin}/signin/reset?token={token}"


def send_link(email: str, purpose: str, url: str) -> None:
    """Send the email. Raises what emailer.send_email raises."""
    if purpose == LINK:
        subject = "Confirm your email for Call to Arms"
        intro = "Confirm this address to add it to your Call to Arms account. You'll be able to sign in with it."
        label = "Confirm email"
        ignore = "If you didn't ask to add this address, ignore this email. Nothing changes without the link."
    elif purpose == PASSWORD_SIGNUP:
        subject = "Confirm your email for Call to Arms"
        intro = "Confirm this address to finish setting up your Call to Arms account."
        label = "Confirm email"
        ignore = ("If you didn't sign up, ignore this email. No account is created until this link is used, "
                  "and whoever tried can't see this message.")
    elif purpose == PASSWORD_RESET:
        subject = "Reset your Call to Arms password"
        intro = "Use this link to choose a new password for your Call to Arms account."
        label = "Choose a new password"
        ignore = ("If you didn't ask for this, ignore this email. Your current password still works and "
                  "nothing changes without the link.")
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


def send_existing_account_notice(email: str, sign_in_url: str) -> None:
    """Someone tried to sign up with an address that already has an account.

    Sent instead of telling the person at the form, which would say whether an
    address has an account here to anyone who asked.
    """
    intro = "Someone tried to create a Call to Arms account with this address, and it already has one."
    body = email_layout.paragraphs(
        f"{intro}\n\nIf that was you, sign in instead. If you've forgotten your password, "
        "you can choose a new one from the sign-in page.\n\n"
        "If it wasn't you, nothing has happened and you can ignore this email."
    )
    html = email_layout.wrap(body, preheader=intro, cta_url=sign_in_url, cta_label="Sign in")
    text = f"{intro}\n\nSign in: {sign_in_url}\n"
    send_email(email, "You already have a Call to Arms account", html, text=text,
               from_addr=sender("onboarding"))

