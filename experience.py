"""Per-(player, system) experience, counted from games actually played.

Why pairings and not signups: a signup is an intention, and people drop. A
pairing is the club's own record that a game was arranged for those two
players, which is the closest thing to "they played" that this app stores.
Byes are excluded — nobody plays a bye.

Why per system: the number an opponent needs is "how much have you played
THIS game", not "how long have you been a member". Someone with thirty Old
World games sitting down to their first Kill Team night is new at Kill Team,
and the whole point of showing this is to set expectations across the table.
(Measured on prod: 85 of 92 players play a single system, so for almost
everyone the two are the same number anyway.)

Counted on demand rather than kept in a column. At this club's scale it's one
indexed join, and a derived value can't drift out of step with the pairings it
comes from — no sweep to run, nothing to rebuild if a pairing is deleted.
"""
from typing import Optional

from sqlalchemy import bindparam, text
from sqlmodel import Session, select

from models import Pairing, PlayerExperienceAdjustment, Signup

# Tier thresholds, in games. Deliberately module-level constants: they're shown
# to players in the UI copy, so the two must not drift.
EXPERIENCED_AT = 10
VETERAN_AT = 20

NEW = "New"
EXPERIENCED = "Experienced"
VETERAN = "Veteran"

# Every tier string this codebase has ever written to Signup.experience.
# "Some" is the retired name for the middle tier and still sits on ~287
# historical signups, so anything reading experience must keep understanding it.
LEGACY_MIDDLE = "Some"


def tier_for(games: int) -> str:
    """The tier a game count falls into."""
    if games >= VETERAN_AT:
        return VETERAN
    if games >= EXPERIENCED_AT:
        return EXPERIENCED
    return NEW


def games_played(db: Session, club_id: int, player_id: Optional[int], system: str) -> int:
    """How many games this player has been paired for in this system.

    Counts a pairing once whether the player was side A or side B, and skips
    byes. Scoped to the club, so a player who plays at two clubs builds a
    count at each — matching how everything else in the multi-club model is
    scoped.
    """
    if player_id is None:
        return 0
    return counts_for_players(db, club_id, system, [player_id]).get(player_id, 0)


def counts_for_players(
    db: Session, club_id: int, system: str, player_ids: list[int]
) -> dict[int, int]:
    """Games played, for many players at once, as ONE aggregate query.

    This used to pull every signup and every pairing for the system into Python
    and count there. That is a full scan of two tables per call — and GET
    /pairings calls it, which the pairings page POLLS while a week is
    unpublished. Measured on prod it cost half a second per request while
    holding the request's database connection, which is how a page nobody
    thought was expensive started exhausting the connection pool.

    Counting in SQL keeps it to one indexed aggregate. Byes are excluded
    (b_signup_id IS NULL) — nobody plays a bye.
    """
    ids = [pid for pid in {p for p in player_ids} if pid is not None]
    if not ids:
        return {}

    rows = db.exec(
        text(
            """
            SELECT s.player_id, count(*) AS games
            FROM pairings pr
            JOIN signups s
              ON s.id = pr.a_signup_id OR s.id = pr.b_signup_id
            WHERE pr.club_id = :club_id
              AND pr.system = :system
              AND pr.b_signup_id IS NOT NULL
              AND s.club_id = :club_id
              AND s.system = :system
              AND s.player_id IN :ids
            GROUP BY s.player_id
            """
        ).bindparams(bindparam("ids", expanding=True)),
        params={"club_id": club_id, "system": system, "ids": ids},
    ).all()
    counted = {pid: n for pid, n in rows}
    return {pid: counted.get(pid, 0) for pid in ids}


def adjustment_for(db: Session, club_id: int, player_id: Optional[int], system: str) -> int:
    """Games this player says they've played elsewhere, for this system.

    An ADDITION rather than a replacement, deliberately. The club's own count
    keeps rising underneath it, so the total never goes stale, and nobody can
    use it to declare themselves permanently new.
    """
    if player_id is None:
        return 0
    row = db.exec(
        select(PlayerExperienceAdjustment)
        .where(PlayerExperienceAdjustment.club_id == club_id)
        .where(PlayerExperienceAdjustment.player_id == player_id)
        .where(PlayerExperienceAdjustment.system == system)
    ).first()
    return row.extra_games if row else 0


def summary(db: Session, club_id: int, player_id: Optional[int], system: str) -> dict:
    """Everything the signup form and the API need about one player's standing
    in one system."""
    tracked = games_played(db, club_id, player_id, system)
    extra = adjustment_for(db, club_id, player_id, system)
    total = tracked + extra
    return {
        "system": system,
        "tracked_games": tracked,
        "extra_games": extra,
        "total_games": total,
        "tier": tier_for(total),
        "experienced_at": EXPERIENCED_AT,
        "veteran_at": VETERAN_AT,
        "next_tier_at": (
            EXPERIENCED_AT if total < EXPERIENCED_AT
            else VETERAN_AT if total < VETERAN_AT
            else None
        ),
    }
