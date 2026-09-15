"""Keeping a week's pairings whole while signups come and go.

Once a week has been paired, every signup in it should sit in exactly one
pairing row: a game, or a BYE. Nothing enforced that. Until 2026-09-15:

- a player who dropped before pairings were published, or who an admin removed,
  had their signup deleted while any generated game they were in stayed behind
  pointing at nothing. The public card then showed "A#836" as a player name, or
  showed their opponent sitting out with no explanation;
- a player who signed up after pairings existed got no row at all, so they had
  no game, no BYE, and did not appear on the unpaired list either;
- deleting a game in the admin grid left both players the same way;
- a player whose opponent dropped always got a new BYE, even when someone else
  was already sitting out that week and the two of them could simply play.

Every path that adds or removes a signup, or moves a player between rows, now
goes through here.

"Paired" means the week is published, or pairings have been generated for it
(any row that is not prearranged exists). Before that, the week has no pairings
to keep whole: a prearranged game losing a player just returns the other player
to the pool, and the next Generate matches them like anyone else.

Nothing here commits. Callers commit once their whole change is made.
"""
from typing import Optional

from sqlmodel import Session, select

from database import scoped
from models import Pairing, PublishState, Signup


def is_published(db: Session, club_id: int, system: str, week: str) -> bool:
    gate = db.exec(
        scoped(PublishState, club_id)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()
    return bool(gate and gate.published)


def is_paired(db: Session, club_id: int, system: str, week: str) -> bool:
    """Whether this week already has pairings that a signup must fit into."""
    if is_published(db, club_id, system, week):
        return True
    generated = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .where(Pairing.prearranged != True)
    ).first()
    return generated is not None


def week_rows(db: Session, club_id: int, system: str, week: str) -> list[Pairing]:
    return list(db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .order_by(Pairing.id)
    ).all())


def add_bye(db: Session, club_id: int, system: str, week: str, su: Signup) -> Pairing:
    row = Pairing(
        week=week, system=system,
        a_signup_id=su.id, b_signup_id=None,
        status="pending", prearranged=False,
        a_faction=su.faction, b_faction=None,
        club_id=club_id,
    )
    db.add(row)
    return row


def waiting_players(
    db: Session, club_id: int, system: str, week: str, exclude_ids: set = frozenset()
) -> list[tuple[Pairing, Signup]]:
    """(BYE row, signup) for everyone sitting out this week with nothing else
    to play, oldest BYE first."""
    rows = week_rows(db, club_id, system, week)
    in_a_game = {
        sid for r in rows if r.b_signup_id is not None
        for sid in (r.a_signup_id, r.b_signup_id)
    }
    out = []
    seen: set[int] = set()
    for r in rows:
        sid = r.a_signup_id
        if r.b_signup_id is not None or sid in in_a_game or sid in exclude_ids or sid in seen:
            continue
        su = db.get(Signup, sid)
        if su is None or su.club_id != club_id:
            continue
        seen.add(sid)
        out.append((r, su))
    return out


def seat(db: Session, club_id: int, system: str, week: str, su: Signup) -> Optional[Signup]:
    """Give `su`, who has no row this week, a game against someone already
    sitting out if a good one is available, and a BYE otherwise.

    Returns the player they now play, or None if they are on a BYE.
    """
    from pairings_engine import pick_waiting_partner

    db.flush()
    waiting = waiting_players(db, club_id, system, week, exclude_ids={su.id})
    partner = pick_waiting_partner(db, week, system, club_id, su, [w for _, w in waiting])
    if partner is None:
        add_bye(db, club_id, system, week, su)
        db.flush()
        return None
    row = next(r for r, w in waiting if w.id == partner.id)
    row.b_signup_id = su.id
    row.b_faction = su.faction
    db.add(row)
    db.flush()
    return partner


def release(
    db: Session, club_id: int, system: str, week: str, signup_ids: set, *, reseat: bool
) -> list[tuple[Signup, Optional[Signup]]]:
    """Take these signups out of the week's pairings, ahead of deleting them.

    Every row they are in is deleted. With `reseat`, each opponent left with
    no row is seated again: a game against someone already sitting out, or a
    BYE. Without it (a week not yet paired) the opponent simply returns to the
    pool for the next Generate.

    Returns (opponent, new partner or None) for every opponent re-seated.
    """
    signup_ids = set(signup_ids)
    rows = [
        r for r in week_rows(db, club_id, system, week)
        if r.a_signup_id in signup_ids or r.b_signup_id in signup_ids
    ]
    opponents: list[int] = []
    for r in rows:
        for sid in (r.a_signup_id, r.b_signup_id):
            if sid is not None and sid not in signup_ids and sid not in opponents:
                opponents.append(sid)
        db.delete(r)
    db.flush()
    if not reseat:
        return []

    moved: list[tuple[Signup, Optional[Signup]]] = []
    for sid in opponents:
        su = db.get(Signup, sid)
        if su is None or su.club_id != club_id:
            continue
        still_placed = any(
            sid in (r.a_signup_id, r.b_signup_id)
            for r in week_rows(db, club_id, system, week)
        )
        if still_placed:
            continue
        moved.append((su, seat(db, club_id, system, week, su)))
    return moved
