"""Accounts and the identities that sign into them (account overhaul Slab 0).

Everything that asks "which account is this sign-in" or "which Discord account
is this user" goes through here, so sign-in, @-mentions and the guild gate all
read the same answer. See ACCOUNT_OVERHAUL.md §7.

`user_identities` is the authority. An account can hold several identities
from one provider; one per provider is primary, and the primary Discord
identity is the account's Discord ID for mentions and the gate.
`users.discord_id` mirrors that primary identity for the expand phase, and is
only read as a fallback for an account with no Discord identity row yet (one
made by the old code between the migration and the deploy). Such an account
gets its row the next time it signs in.

Imports models only, never database, so database.py can use it.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlmodel import Session, select

from models import User, UserIdentity

DISCORD = "discord"


@dataclass
class ProviderProfile:
    """What a provider tells us about the person signing in."""
    provider: str
    subject: str  # the provider's stable user ID
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    email: Optional[str] = None
    email_verified: bool = False

    def to_payload(self) -> dict:
        return {
            "provider": self.provider,
            "subject": self.subject,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "email": self.email,
            "email_verified": self.email_verified,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> Optional["ProviderProfile"]:
        """Read a pending-signup payload, new shape or the Discord-only one
        that predates it (a cookie issued just before the deploy lives ten
        minutes)."""
        if payload.get("provider") and payload.get("subject"):
            return cls(
                provider=str(payload["provider"]),
                subject=str(payload["subject"]),
                name=payload.get("name"),
                avatar_url=payload.get("avatar_url"),
                email=payload.get("email"),
                email_verified=bool(payload.get("email_verified")),
            )
        if payload.get("discord_id"):
            return cls(
                provider=DISCORD,
                subject=str(payload["discord_id"]),
                name=payload.get("discord_name"),
                avatar_url=payload.get("avatar_url"),
            )
        return None


def find_identity(db: Session, provider: str, subject: str) -> Optional[UserIdentity]:
    return db.exec(
        select(UserIdentity)
        .where(UserIdentity.provider == provider)
        .where(UserIdentity.provider_user_id == subject)
    ).first()


def find_user_for_profile(db: Session, profile: ProviderProfile) -> Optional[User]:
    """The account this sign-in belongs to, or None for someone new.

    Falls back to users.discord_id for a Discord sign-in with no identity row,
    and creates the row, so accounts the old code made heal themselves.
    """
    ident = find_identity(db, profile.provider, profile.subject)
    if ident is not None:
        return db.get(User, ident.user_id)
    if profile.provider != DISCORD:
        return None
    user = db.exec(select(User).where(User.discord_id == profile.subject)).first()
    if user is None or identity_for(db, user.id, DISCORD) is not None:
        # A mirror that disagrees with a real identity row is stale; the row wins.
        return None
    attach_identity(db, user, profile)
    return user


def identity_for(db: Session, user_id: int, provider: str) -> Optional[UserIdentity]:
    """The account's primary identity from `provider`, or None."""
    return db.exec(
        select(UserIdentity)
        .where(UserIdentity.user_id == user_id)
        .where(UserIdentity.provider == provider)
        .where(UserIdentity.is_primary == True)  # noqa: E712
    ).first()


def attach_identity(db: Session, user: User, profile: ProviderProfile) -> UserIdentity:
    """Record that `profile` signs into `user`. Caller commits.

    Refuses to move an identity that already belongs to someone else: joining
    two accounts is a merge (Slab 1), never a side effect of signing in. A new
    identity is primary only if the account has none from that provider yet;
    a second one signs in but doesn't change which account gets mentioned.
    """
    ident = find_identity(db, profile.provider, profile.subject)
    if ident is not None and ident.user_id != user.id:
        raise ValueError(
            f"{profile.provider} identity already belongs to user {ident.user_id}"
        )
    if ident is None:
        ident = UserIdentity(
            user_id=user.id,
            provider=profile.provider,
            provider_user_id=profile.subject,
            is_primary=identity_for(db, user.id, profile.provider) is None,
        )
    refresh_identity(ident, profile)
    db.add(ident)
    if profile.provider == DISCORD and ident.is_primary:
        user.discord_id = profile.subject
        db.add(user)
    return ident


def refresh_identity(ident: UserIdentity, profile: ProviderProfile) -> None:
    """Take the provider's latest name, avatar and email. None of these are
    the user's own choices, so overwriting them is always correct."""
    ident.name = profile.name
    ident.avatar_url = profile.avatar_url
    if profile.email:
        ident.email = profile.email
        ident.email_verified = profile.email_verified
    ident.last_used_at = datetime.utcnow()


def record_sign_in(db: Session, user: User, profile: ProviderProfile) -> None:
    """A returning sign-in. Caller commits.

    Only provider-owned fields change. users.discord_name and avatar_url mirror
    the primary Discord identity, so they follow a sign-in with that and nothing
    else; display_name is never touched here (that was the trap in
    ACCOUNT_OVERHAUL.md §2).
    """
    ident = attach_identity(db, user, profile)
    if profile.provider == DISCORD and ident.is_primary:
        user.discord_name = profile.name
        user.avatar_url = profile.avatar_url
    user.last_login_at = datetime.utcnow()
    db.add(user)


def create_user_for_profile(db: Session, profile: ProviderProfile, **fields) -> User:
    """A new account signing in with `profile`. Caller commits.

    `fields` carries the rest of the row (club_id, home_club_id, flags).
    """
    user = User(
        discord_id=profile.subject if profile.provider == DISCORD else None,
        discord_name=profile.name if profile.provider == DISCORD else None,
        avatar_url=profile.avatar_url,
        **fields,
    )
    db.add(user)
    db.flush()
    attach_identity(db, user, profile)
    return user


def discord_ids_for_users(db: Session, user_ids: Iterable[int]) -> dict[int, str]:
    """user_id -> primary Discord user ID, for every account with a Discord identity.

    The one read path for "this user's Discord account": mentions and the
    guild gate both use it, so unlinking Discord takes effect everywhere at
    once.
    """
    ids = {uid for uid in user_ids if uid}
    if not ids:
        return {}
    found = {
        i.user_id: i.provider_user_id
        for i in db.exec(
            select(UserIdentity)
            .where(UserIdentity.user_id.in_(ids))
            .where(UserIdentity.provider == DISCORD)
            .where(UserIdentity.is_primary == True)  # noqa: E712
        ).all()
    }
    missing = ids - set(found)
    if missing:
        # Accounts with no identity rows at all haven't been backfilled or
        # seen since the deploy. One with any identity row is authoritative
        # and must not be second-guessed by a stale mirror.
        has_any = {
            i.user_id for i in db.exec(
                select(UserIdentity).where(UserIdentity.user_id.in_(missing))
            ).all()
        }
        for u in db.exec(select(User).where(User.id.in_(missing - has_any))).all():
            if u.discord_id:
                found[u.id] = u.discord_id
    return found


def discord_id_for_user(db: Session, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    return discord_ids_for_users(db, [user_id]).get(user_id)


def display_name_for(user: User) -> str:
    """The name to greet someone by: theirs if they set one, else their
    Discord handle."""
    return user.display_name or user.discord_name or "Player"


def bump_session_version(db: Session, user: User) -> None:
    """End every session this account has. Caller commits."""
    user.session_version = (user.session_version or 0) + 1
    db.add(user)
