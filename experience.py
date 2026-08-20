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
    # The player's signup ids for this (club, system) — the join key pairings
    # actually store.
    signup_ids = db.exec(
        select(Signup.id)
        .where(Signup.club_id == club_id)
        .where(Signup.system == system)
        .where(Signup.player_id == player_id)
    ).all()
    if not signup_ids:
        return 0
    ids = set(signup_ids)

    rows = db.exec(
        select(Pairing.a_signup_id, Pairing.b_signup_id)
        .where(Pairing.club_id == club_id)
        .where(Pairing.system == system)
        .where(Pairing.b_signup_id.is_not(None))  # a bye isn't a game
    ).all()
    return sum(1 for a, b in rows if a in ids or b in ids)


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
