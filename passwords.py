"""Passwords: hashing, strength, and the limits around guessing them
(account overhaul Slab 8).

What is stored, and what nobody can read
----------------------------------------
Only an Argon2id hash, in `password_credentials`. Nobody can read a password
out of it: not an admin, not whoever holds a database backup, not us. Nothing
logs passwords, no email contains one, and no endpoint ever returns one.

Argon2id parameters are 32 MiB, 2 passes, 1 lane: above the OWASP baseline
(19 MiB, t=2) and ~80ms on the Fly machine. Memory is the point of Argon2, and
also the thing to be careful with here: this machine has 512 MB and has been
wedged by memory before, and FastAPI runs sync endpoints in a 40-thread pool.
So hashing goes through a semaphore of 4, capping it at ~128 MB however many
people sign in at once. The rate limits in auth.py do the rest.

A hash carries its own parameters, so raising them later is safe: an old hash
still verifies, and `verify` hands back a fresh hash to store when it sees one
made with weaker settings.

Weak and breached passwords
---------------------------
Minimum ten characters, and no composition rules (OWASP): they push people
towards "Passw0rd!" and nothing else. Instead the password is checked against
Have I Been Pwned's range API, which takes the first five characters of its
SHA-1 and returns every matching suffix, so the password itself never leaves
this server. If that service is unreachable the password is allowed: nobody
gets locked out of signing up because someone else's API is down.
"""
import hashlib
import threading
from typing import Optional

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

MIN_LENGTH = 10
# Argon2 handles long inputs fine; the cap is so a megabyte of "password" can't
# be used to make the server do work.
MAX_LENGTH = 128

_hasher = PasswordHasher(memory_cost=32768, time_cost=2, parallelism=1,
                         hash_len=32, salt_len=16, type=Type.ID)
# See the note above: bounds Argon2's memory whatever the thread pool is doing.
_slots = threading.BoundedSemaphore(4)

HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/"
HIBP_TIMEOUT = 3.0


class PasswordRejected(Exception):
    """Not good enough, with a message fit to show the person."""


def hash_password(password: str) -> str:
    with _slots:
        return _hasher.hash(password)


def verify_password(stored: str, password: str) -> tuple[bool, Optional[str]]:
    """(matches, a fresh hash to store if the old one used weaker settings)."""
    with _slots:
        try:
            _hasher.verify(stored, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, None
        try:
            if _hasher.check_needs_rehash(stored):
                return True, _hasher.hash(password)
        except InvalidHashError:
            pass
        return True, None


def breach_count(password: str) -> Optional[int]:
    """How many breaches this password is known from, or None if the service
    couldn't be reached. Only the first five characters of the SHA-1 are sent."""
    digest = hashlib.sha1(password.encode()).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    try:
        resp = httpx.get(
            HIBP_RANGE_URL + prefix,
            headers={"Add-Padding": "true", "User-Agent": "call-to-arms"},
            timeout=HIBP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
    except Exception:
        return None
    for line in resp.text.splitlines():
        found, _, count = line.strip().partition(":")
        if found == suffix:
            try:
                return int(count)
            except ValueError:
                return 1
    return 0


def check_strength(password: str, email: Optional[str] = None) -> None:
    """Raise PasswordRejected if this password shouldn't be used."""
    if not password or len(password) < MIN_LENGTH:
        raise PasswordRejected(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordRejected(f"Keep it under {MAX_LENGTH} characters.")
    if email:
        local = email.split("@", 1)[0].strip().lower()
        if password.strip().lower() in (email.strip().lower(), local) and local:
            raise PasswordRejected("That's your email address. Pick something else.")
    if breach_count(password):
        raise PasswordRejected(
            "That password appears in a known data breach, so it isn't safe to use. Pick another."
        )
