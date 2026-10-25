"""In-app notices: the news a player who isn't on Discord would otherwise miss.

The club hears about a publish or a drop-out in its Discord channel. Everyone
else finds out by chance. These are the same two events, recorded per ACCOUNT
and shown once as a dismissible banner on the player's next page load.

Per account rather than per browser deliberately. A browser could work out on
its own that a new week has been published, but nothing in a published week
changes shape when your opponent drops out, so only the server can know that
happened to you. Keying on the account also means reading it on your phone
clears it on your laptop.

Writers are `notify_pairings_published` and `notify_opponent_dropped`, called
from the publish endpoint and from the two drop paths. Both swallow nothing
themselves: callers wrap them, because a banner must never be able to fail a
publish or strand a drop-out half done.
"""
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

import push
from auth import active_club_id, require_user
from database import get_session, scoped
from models import PlayerNotice, Player, Signup, User

router = APIRouter(prefix="/notices", tags=["notices"])

PAIRINGS_PUBLISHED = "pairings_published"
OPPONENT_DROPPED = "opponent_dropped"
GAME_FOUND = "game_found"
# Not a week's news: the outcome of a faction request (faction_requests.py).
# Stored with week "".
FACTION_REQUEST = "faction_request"
# Pick-up games (pickups.py): a match proposed, confirmed, or fallen through.
# Stored with week "".
PICKUP = "pickup"

# How long a notice is worth showing. A week's news is stale once the night has
# been and gone, and nobody wants a banner about a game they already played.
SHOW_FOR = timedelta(days=10)
# How many to show at once, so a busy week can't bury the page in banners.
MAX_SHOWN = 4
# Rows this old are deleted as new ones arrive, instead of needing a cron.
KEEP_FOR = timedelta(days=60)


def week_label(week: str) -> str:
    """"23/09/2026" -> "Wed 23 Sep". Unparseable weeks pass straight through."""
    try:
        return datetime.strptime(week.strip(), "%d/%m/%Y").strftime("%a %d %b")
    except ValueError:
        return week


def _owner_ids(db: Session, club_id: int, player_ids: list[int]) -> dict[int, int]:
    """player_id -> owning user_id, for the players that have an account.

    Guests (no player row) and unlinked players simply aren't in the result:
    there is nobody to show a banner to.
    """
    ids = [p for p in player_ids if p is not None]
    if not ids:
        return {}
    rows = db.exec(
        scoped(Player, club_id).where(Player.id.in_(ids)).where(Player.user_id != None)  # noqa: E711
    ).all()
    return {p.id: p.user_id for p in rows}


def _prune(db: Session, club_id: int) -> None:
    cutoff = datetime.utcnow() - KEEP_FOR
    for old in db.exec(
        scoped(PlayerNotice, club_id).where(PlayerNotice.created_at < cutoff)
    ).all():
        db.delete(old)


def record(
    db: Session,
    *,
    user_id: int,
    club_id: int,
    system: str,
    week: str,
    kind: str,
    body: str,
    once: bool = False,
    url: Optional[str] = None,
) -> Optional[PlayerNotice]:
    """Add one notice. `once` skips it if this account already has that kind of
    notice for that week and system (unpublish-then-republish must not notify
    twice). Flushed, not committed: the caller's transaction owns it."""
    if once:
        existing = db.exec(
            scoped(PlayerNotice, club_id)
            .where(PlayerNotice.user_id == user_id)
            .where(PlayerNotice.system == system)
            .where(PlayerNotice.week == week)
            .where(PlayerNotice.kind == kind)
        ).first()
        if existing is not None:
            return None
    row = PlayerNotice(
        user_id=user_id, club_id=club_id, system=system, week=week, kind=kind, body=body
    )
    db.add(row)
    db.flush()
    # The same news to the account's phone, sent once this transaction commits
    # (push.py). Week news opens that night's pairings when tapped.
    if url is None:
        url = f"/pairings?{urlencode({'system': system, 'week': week})}" if week else "/"
    push.queue(db, user_id=user_id, kind=kind, body=body, url=url, tag=f"{kind}:{system}:{week}")
    return row


def notify_pairings_published(db: Session, club_id: int, system: str, week: str) -> int:
    """One notice per account playing in the week that has just been published.

    Only people in the week: a publish is not club news, it is "your game is
    up". Returns how many were written.
    """
    signups = db.exec(
        scoped(Signup, club_id).where(Signup.system == system).where(Signup.week == week)
    ).all()
    owners = _owner_ids(db, club_id, [s.player_id for s in signups])
    body = f"{system} pairings for {week_label(week)} are up."
    written = 0
    for user_id in dict.fromkeys(owners.values()):
        if record(
            db, user_id=user_id, club_id=club_id, system=system, week=week,
            kind=PAIRINGS_PUBLISHED, body=body, once=True,
        ):
            written += 1
    if written:
        _prune(db, club_id)
    return written


def notify_opponent_dropped(
    db: Session, club_id: int, system: str, week: str, moved: list, dropped_name: str
) -> int:
    """One notice per player re-seated by a drop-out from a published week.

    `moved` is week_pairings.release's (player, new opponent or None), so this
    says the same thing as the Discord post: who left, and what you are playing
    now. Not fired for an unpublished week, where nobody had been told their
    game in the first place.
    """
    pairs = list(moved)
    label = week_label(week)
    # Both halves of a re-seated game are told, not just the player who lost
    # their opponent: the one who was sitting out has gained a game, and that
    # is news to them as well.
    told: list[tuple[int, str]] = []
    player_ids = [su.player_id for su, _ in pairs] + [
        p.player_id for _, p in pairs if p is not None
    ]
    owners = _owner_ids(db, club_id, player_ids)
    for su, partner in pairs:
        if partner is not None:
            told.append((
                su.player_id,
                f"{dropped_name} dropped out of {label}, so you are now playing "
                f"{partner.player_name}, who was sitting out.",
            ))
            told.append((
                partner.player_id,
                f"{dropped_name} dropped out of {label}, so you are playing "
                f"{su.player_name} instead of sitting out.",
            ))
        else:
            told.append((
                su.player_id,
                f"{dropped_name} dropped out of {label}, so you are without an "
                f"opponent. Head to the pairings page to re-arrange your game.",
            ))

    written = 0
    for player_id, body in told:
        user_id = owners.get(player_id)
        if user_id is None:
            continue
        if record(
            db, user_id=user_id, club_id=club_id, system=system, week=week,
            kind=OPPONENT_DROPPED, body=body,
        ):
            written += 1
    if written:
        _prune(db, club_id)
    return written


def notify_game_found(
    db: Session, club_id: int, system: str, week: str, partner: Signup, joiner_name: str
) -> int:
    """A signup arrived after a published week's pairings went out and gave
    someone who was sitting out a game. That person's night has changed, so
    they get the same news the channel gets."""
    owners = _owner_ids(db, club_id, [partner.player_id])
    user_id = owners.get(partner.player_id)
    if user_id is None:
        return 0
    body = (
        f"{joiner_name} signed up after pairings went out, so you have a game "
        f"on {week_label(week)} after all."
    )
    if record(
        db, user_id=user_id, club_id=club_id, system=system, week=week,
        kind=GAME_FOUND, body=body,
    ):
        _prune(db, club_id)
        return 1
    return 0


def _serialise(n: PlayerNotice) -> dict:
    return {
        "id": n.id,
        "kind": n.kind,
        "system": n.system,
        "week": n.week,
        "body": n.body,
        "created_at": n.created_at.isoformat(),
    }


@router.get("")
def list_notices(
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Unseen notices for this account at the club it is browsing, newest first."""
    cutoff = datetime.utcnow() - SHOW_FOR
    rows = db.exec(
        scoped(PlayerNotice, club_id)
        .where(PlayerNotice.user_id == user.id)
        .where(PlayerNotice.seen_at == None)  # noqa: E711
        .where(PlayerNotice.created_at >= cutoff)
        .order_by(PlayerNotice.created_at.desc())
        .limit(MAX_SHOWN)
    ).all()
    return {"notices": [_serialise(n) for n in rows]}


class SeenBody(BaseModel):
    ids: list[int] = []


@router.post("/seen")
def mark_seen(
    body: SeenBody,
    user: User = Depends(require_user),
    club_id: int = Depends(active_club_id),
    db: Session = Depends(get_session),
):
    """Dismiss. Only ever touches this account's own rows, so an id from
    somewhere else is a no-op rather than an error."""
    if not body.ids:
        return {"ok": True, "seen": 0}
    rows = db.exec(
        scoped(PlayerNotice, club_id)
        .where(PlayerNotice.user_id == user.id)
        .where(PlayerNotice.id.in_(body.ids[:MAX_SHOWN * 4]))
        .where(PlayerNotice.seen_at == None)  # noqa: E711
    ).all()
    now = datetime.utcnow()
    for r in rows:
        r.seen_at = now
        db.add(r)
    db.commit()
    return {"ok": True, "seen": len(rows)}
