"""Per-system levelling, 1 to 60, derived from games played.

A progression toy, deliberately separate from the New/Experienced/Veteran tiers
in experience.py. Those answer "what am I sitting down to" and feed the
matcher; this answers "how far have I come" and feeds nothing but the player's
own satisfaction. Nothing here influences pairing.

THE CURVE. Chosen against this club's real play rate rather than a guess:
the most consistent player manages ~45 games a year, most manage far fewer.
Cost to gain a level starts at 2 games and grows 2% per level, so L2 costs 2
games, L30 costs 3, L59 costs 6 — gentle, but always increasing. Level 60
lands at ~220 games: roughly five years for the club's most active player and
out of reach for most, which is the point. A cap everyone hits in a season
isn't a landmark.

Note the original brief asked for 2-3 games per early level, exponential
growth, AND level 60 within two years. Those can't hold together — 59 levels
at even a flat 2 games each is 118 games, already 2.6 years at 45 games/year,
and any growth makes it longer. The timeline gave way; the feel was kept.

XP COMES FROM PAIRINGS ONLY. Not signups (you can sign up and drop), and
NOT the "games played elsewhere" adjustment that feeds the experience tiers —
that one is self-declared, and a self-declared number must never move a
progression bar or someone can type their way to level 60.
"""
from typing import Optional

import httpx
from sqlmodel import Session, select

from database import name_with_mention, resolve_webhook_url
from experience import games_played
from models import PlayerLevelAnnouncement, Player, SystemConfig
from observability import capture

LEVEL_CAP = 60
_FIRST_LEVEL_COST = 2      # games to go from level 1 to level 2
_GROWTH = 1.02             # per level

# Milestones worth announcing. Every level would be noise — a club of 100
# players crossing a level every couple of games would bury the channel.
ANNOUNCE_EVERY = 10


def _cost(level: int) -> int:
    """Games needed to get from `level` to `level + 1`."""
    return max(1, round(_FIRST_LEVEL_COST * (_GROWTH ** (level - 1))))


def _thresholds() -> list[int]:
    """Cumulative games required to REACH each level, indexed by level.

    Built once at import — 60 entries, and every level lookup is then a walk
    over a tiny list rather than repeated exponentiation.
    """
    out = [0, 0]  # index 0 unused; level 1 needs 0 games
    total = 0
    for lv in range(1, LEVEL_CAP):
        total += _cost(lv)
        out.append(total)
    return out


THRESHOLDS = _thresholds()
GAMES_FOR_CAP = THRESHOLDS[LEVEL_CAP]


def level_for(games: int) -> int:
    """The level a game count reaches. Capped at LEVEL_CAP."""
    lv = 1
    for candidate in range(2, LEVEL_CAP + 1):
        if games >= THRESHOLDS[candidate]:
            lv = candidate
        else:
            break
    return lv


# Rarity bands, borrowed from the game this is modelled on. Kept here rather
# than in the frontend so the Discord announcement and the profile agree.
def band_for(level: int) -> str:
    if level >= LEVEL_CAP:
        return "legendary"
    if level >= 50:
        return "epic"
    if level >= 30:
        return "rare"
    return "common"


def progress(db: Session, club_id: int, player_id: Optional[int], system: str) -> dict:
    """A player's standing in one system: level, band, and progress toward the
    next level."""
    games = games_played(db, club_id, player_id, system)
    lv = level_for(games)
    at_cap = lv >= LEVEL_CAP

    into = games - THRESHOLDS[lv]
    needed = 0 if at_cap else THRESHOLDS[lv + 1] - THRESHOLDS[lv]
    return {
        "system": system,
        "games": games,
        "level": lv,
        "band": band_for(lv),
        "at_cap": at_cap,
        "level_cap": LEVEL_CAP,
        # Progress through the CURRENT level, which is what a bar should show —
        # not progress toward 60, which would sit at 3% for years.
        "games_into_level": into,
        "games_for_level": needed,
        "games_to_next": 0 if at_cap else needed - into,
        "percent": 100 if at_cap else (round(into * 100 / needed) if needed else 0),
    }


def milestones_crossed(previous_level: int, current_level: int) -> list[int]:
    """Announceable levels passed between two points.

    Returns every multiple of ANNOUNCE_EVERY in (previous, current], plus the
    cap itself — so a player who jumps several levels at once (a backfill, or a
    catch-up week) gets one announcement per milestone rather than none, and a
    player who reaches 60 is always announced even though 60 is a multiple
    anyway.
    """
    out = [
        lv for lv in range(previous_level + 1, current_level + 1)
        if lv % ANNOUNCE_EVERY == 0 or lv == LEVEL_CAP
    ]
    return sorted(set(out))


_BAND_FLOURISH = {
    "common": "🎉",
    "rare": "🔷",
    "epic": "🟣",
    "legendary": "🟠",
}


def announce_level_ups(db: Session, club_id: int, system: str) -> int:
    """Post a "ding" for every announceable level crossed since last time.

    Called after pairings are published — the moment a week's games become
    official, and the same moment the pairings themselves get posted, so the
    celebration lands with the news rather than at some arbitrary hour.

    Returns how many announcements were posted. Never raises: a failed webhook
    must not take down the publish that triggered it.
    """
    config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    url = resolve_webhook_url(db, club_id, "level_up", config.id if config else None)

    players = db.exec(
        select(Player).where(Player.club_id == club_id).where(Player.active == True)
    ).all()

    posted = 0
    for player in players:
        state = db.exec(
            select(PlayerLevelAnnouncement)
            .where(PlayerLevelAnnouncement.club_id == club_id)
            .where(PlayerLevelAnnouncement.player_id == player.id)
            .where(PlayerLevelAnnouncement.system == system)
        ).first()
        previous = state.last_level if state else 1
        current = level_for(games_played(db, club_id, player.id, system))
        if current <= previous:
            continue

        crossed = milestones_crossed(previous, current)

        # The high-water mark advances whether or not anything was announced,
        # so a player who climbs from 11 to 19 doesn't get a stale "ding" for
        # level 20 the moment a webhook is finally configured.
        if state is None:
            state = PlayerLevelAnnouncement(
                club_id=club_id, player_id=player.id, system=system, last_level=current
            )
        else:
            state.last_level = current
        db.add(state)

        if url and crossed:
            for lv in crossed:
                if _post_ding(url, db, player, system, lv):
                    posted += 1
    db.commit()
    return posted


def _post_ding(url: str, db: Session, player: Player, system: str, level: int) -> bool:
    band = band_for(level)
    who = name_with_mention(db, player.name, player.id)
    if level >= LEVEL_CAP:
        text = f"{_BAND_FLOURISH[band]} **DING!** {who} has hit **level {LEVEL_CAP}** in {system}. Maximum level. Nothing left to prove."
    else:
        text = f"{_BAND_FLOURISH[band]} **DING!** {who} just reached **level {level}** in {system}."
    try:
        resp = httpx.post(
            url,
            json={"content": text, "allowed_mentions": {"parse": ["users"]}},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        if resp.status_code >= 400:
            capture(
                RuntimeError(f"level_up webhook returned {resp.status_code}"),
                kind="level_up_webhook", status=resp.status_code,
            )
            return False
        return True
    except Exception as e:
        capture(e, kind="level_up_webhook")
        return False
