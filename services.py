"""Shared business logic, independent of the API layer.

Functions here compute things from data. They don't know about FastAPI,
requests, or responses — just take a session and inputs, return outputs.
"""
import json
from typing import Iterable, Optional

import httpx
from sqlmodel import Session, select, or_

from database import resolve_webhook_url
from models import LeagueResult, Signup, Player


def compute_league_record(player_id: int, results: Iterable[LeagueResult]) -> dict:
    """Count wins/losses/draws across the given league results."""
    wins = losses = draws = 0
    for r in results:
        is_p1 = r.player_1_id == player_id
        if r.result == "Draw":
            draws += 1
        elif r.result == "Player 1 Victory":
            wins += int(is_p1)
            losses += int(not is_p1)
        elif r.result == "Player 2 Victory":
            wins += int(not is_p1)
            losses += int(is_p1)
    return {"wins": wins, "losses": losses, "draws": draws, "total_games": wins + losses + draws}


def fetch_player_results(session: Session, player_id: int, club_id: int) -> list[LeagueResult]:
    """Return all league results involving the given player, newest first."""
    stmt = (
        select(LeagueResult)
        .where(LeagueResult.club_id == club_id)
        .where(or_(LeagueResult.player_1_id == player_id, LeagueResult.player_2_id == player_id))
        .order_by(LeagueResult.created_at.desc())
    )
    return session.exec(stmt).all()


def fetch_player_signups(session: Session, player_id: int, club_id: int) -> list[Signup]:
    """Return ALL signups for a player across all weeks/systems."""
    stmt = select(Signup).where(Signup.club_id == club_id).where(Signup.player_id == player_id)
    return session.exec(stmt).all()


def signup_counts_per_system(signups: Iterable[Signup]) -> dict[str, int]:
    """How many sessions has the player signed up to, per game system?"""
    counts: dict[str, int] = {}
    for s in signups:
        counts[s.system] = counts.get(s.system, 0) + 1
    return counts


def faction_usage_per_system(signups: Iterable[Signup]) -> dict[str, dict[str, int]]:
    """Per system, how many times the player signed up with each named faction.

    Unset/blank factions are ignored — only counts faction picks.
    """
    out: dict[str, dict[str, int]] = {}
    for s in signups:
        if not s.faction:
            continue
        per_sys = out.setdefault(s.system, {})
        per_sys[s.faction] = per_sys.get(s.faction, 0) + 1
    return out


def league_faction_counts(player_id: int, results: Iterable[LeagueResult]) -> dict[str, int]:
    """How many league games the player has played with each faction."""
    counts: dict[str, int] = {}
    for r in results:
        fac = r.player_1_faction if r.player_1_id == player_id else r.player_2_faction
        if fac:
            counts[fac] = counts.get(fac, 0) + 1
    return counts


def build_elo_history(player_id: int, results: Iterable[LeagueResult]) -> list[dict]:
    """Build [{date, elo}, ...] starting at 1000, oldest first.

    Each subsequent entry uses the player's rating_after from that result.
    """
    # results came in newest-first; reverse to chronological
    chrono = list(results)[::-1]
    history = [{"date": None, "elo": 1000, "label": "Start"}]
    for r in chrono:
        is_p1 = r.player_1_id == player_id
        after = r.player_1_rating_after if is_p1 else r.player_2_rating_after
        if after is None:
            continue
        history.append({
            "date": r.result_date,
            "elo": round(float(after)),
            "label": f"Game {len(history)}"
        })
    return history


def first_league_winner_id(session: Session) -> Optional[int]:
    """Returns the player_id of the player who won the very first non-draw league game."""
    stmt = select(LeagueResult).where(LeagueResult.result != "Draw").order_by(LeagueResult.created_at).limit(1)
    first = session.exec(stmt).first()
    if first is None:
        return None
    if first.result == "Player 1 Victory":
        return first.player_1_id
    if first.result == "Player 2 Victory":
        return first.player_2_id
    return None


def compute_achievements(
    player_id: int,
    record: dict,
    results: list[LeagueResult],
    faction_usage: dict[str, dict[str, int]],
    elo_history: list[dict],
    first_winner_id: Optional[int],
) -> list[str]:
    """Compute the list of achievement labels the player has earned."""
    achievements: list[str] = []

    games = record.get("total_games", 0)

    # League-based
    if games > 0:
        if first_winner_id is not None and first_winner_id == player_id:
            achievements.append("First Blood")

        if games >= 5:
            achievements.append("Grizzled")
        if games >= 10:
            achievements.append("Battle-Hardened")

        # Streaks
        max_streak = cur_streak = 0
        # results are newest-first; for streak detection, walk chronologically
        for r in reversed(results):
            is_p1 = r.player_1_id == player_id
            won = (r.result == "Player 1 Victory" and is_p1) or (r.result == "Player 2 Victory" and not is_p1)
            if won:
                cur_streak += 1
                max_streak = max(max_streak, cur_streak)
            else:
                cur_streak = 0
        if max_streak >= 3:
            achievements.append("Hat-Trick")
        if max_streak >= 5:
            achievements.append("Unstoppable")

        # Loyalist: 5+ league games with the same faction
        fac_counts = league_faction_counts(player_id, results)
        if fac_counts and max(fac_counts.values()) >= 5:
            achievements.append("Loyalist")

        # Giant Slayer: won a league game vs an opponent 100+ ELO higher
        for r in results:
            is_p1 = r.player_1_id == player_id
            won = (r.result == "Player 1 Victory" and is_p1) or (r.result == "Player 2 Victory" and not is_p1)
            if not won:
                continue
            my_before = r.player_1_rating_before if is_p1 else r.player_2_rating_before
            opp_before = r.player_2_rating_before if is_p1 else r.player_1_rating_before
            if my_before is None or opp_before is None:
                continue
            if (opp_before - my_before) >= 100:
                achievements.append("Giant Slayer")
                break

        # Climber: peaked at 1100+
        peak = max((h["elo"] for h in elo_history), default=0)
        if peak >= 1100:
            achievements.append("Climber")

    # Cross-system
    distinct_factions = set()
    total_signups = 0
    for facs in (faction_usage or {}).values():
        distinct_factions.update(facs.keys())
        total_signups += sum(facs.values())

    if len(distinct_factions) >= 3:
        achievements.append("Diversifier")
    if len(distinct_factions) >= 5:
        achievements.append("Generalist")
    if total_signups >= 20:
        achievements.append("Veteran")
    if total_signups >= 100:
        achievements.append("Centurion")
    if 1 <= total_signups <= 4:
        achievements.append("Newcomer")
    if 5 <= total_signups <= 19:
        achievements.append("Familiar Face")

    if len(faction_usage or {}) >= 2:
        achievements.append("Hobby Hopper")
    if len(faction_usage or {}) >= 3:
        achievements.append("Triple Threat")

    return achievements


def player_titles(player: Player) -> list[str]:
    """Decode the JSON titles list, returning [] if not set or invalid."""
    raw = getattr(player, "titles", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t) for t in parsed if str(t).strip()]
    except Exception:
        pass
    return []


def set_player_titles(player: Player, titles: list[str]) -> None:
    cleaned = [t.strip() for t in titles if t and t.strip()]
    player.titles = json.dumps(cleaned) if cleaned else None


# Tooltip text for each achievement, shown in the UI.
ACHIEVEMENT_DESCRIPTIONS: dict[str, str] = {
    "First Blood": "Was the very first player to win an Old World League game.",
    "Grizzled": "Played 5 or more Old World League games.",
    "Battle-Hardened": "Played 10 or more Old World League games.",
    "Hat-Trick": "Won 3 or more Old World League games in a row.",
    "Unstoppable": "Won 5 or more Old World League games in a row.",
    "Loyalist": "Played 5 or more league games with the same faction.",
    "Giant Slayer": "Won a league game against an opponent rated 100+ ELO higher than you.",
    "Climber": "Reached 1100 ELO or higher at any point.",
    "Diversifier": "Used 3 or more different factions across all systems.",
    "Generalist": "Used 5 or more different factions across all systems.",
    "Veteran": "Reached 20 total signups across all systems.",
    "Centurion": "Reached 100 total signups across all systems.",
    "Newcomer": "Has signed up to 4 or fewer sessions so far.",
    "Familiar Face": "Reached 5 total signups across all systems.",
    "Hobby Hopper": "Has signed up for at least 2 different game systems.",
    "Triple Threat": "Has signed up for 3 different game systems.",
}

# Subset of achievements eligible for Discord announcement.
# Cross-system / signup-volume achievements like "Hobby Hopper" stay silent.
LEAGUE_ANNOUNCED_ACHIEVEMENTS: set[str] = {
    "First Blood",
    "Grizzled",
    "Battle-Hardened",
    "Hat-Trick",
    "Unstoppable",
    "Loyalist",
    "Giant Slayer",
    "Climber",
}


def _player_announced_achievements(player: Player) -> list[str]:
    """JSON-decode Player.announced_achievements; return [] if empty/invalid."""
    raw = getattr(player, "announced_achievements", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(a) for a in parsed]
    except Exception:
        pass
    return []


def _set_player_announced_achievements(player: Player, names: Iterable[str]) -> None:
    """JSON-encode sorted(set(names)) into Player.announced_achievements.

    Always writes a valid JSON array so None (never snapshotted) vs '[]'
    (snapshotted with zero) remain distinguishable.
    """
    player.announced_achievements = json.dumps(sorted(set(names)))


def post_discord_achievement(player_name: str, achievement: str, club_id: int, db: Session) -> None:
    """Post an achievement unlock message to Discord. No-op if webhook unset."""
    url = resolve_webhook_url(db, club_id, "achievement")
    if not url:
        return
    lines = [
        "🏅 **Achievement Unlocked!**",
        f"**{player_name}** earned **{achievement}**",
    ]
    desc = ACHIEVEMENT_DESCRIPTIONS.get(achievement)
    if desc:
        lines.append(f"*{desc}*")
    try:
        httpx.post(url, json={"content": "\n".join(lines)}, timeout=5.0)
    except Exception:
        pass


def announce_new_achievements(db: Session, player_id: int) -> None:
    """Check for newly earned league achievements and post to Discord.

    Best-effort: the whole function is wrapped in try/except so a failure
    never breaks the caller (result submission, etc.).

    First call per player silently snapshots their current state — pre-existing
    players don't receive a retroactive flood of notifications.
    """
    try:
        player = db.get(Player, player_id)
        if player is None:
            return

        webhook_url = resolve_webhook_url(db, player.club_id, "achievement")
        if not webhook_url:
            return

        results = fetch_player_results(db, player_id, player.club_id)
        record = compute_league_record(player_id, results)
        elo_history = build_elo_history(player_id, results)
        signups = fetch_player_signups(db, player_id, player.club_id)
        fac_usage = faction_usage_per_system(signups)
        first_winner = first_league_winner_id(db)

        current = set(compute_achievements(player_id, record, results, fac_usage, elo_history, first_winner))
        eligible_current = current & LEAGUE_ANNOUNCED_ACHIEVEMENTS

        if player.announced_achievements is None:
            # First snapshot — backfill silently, no Discord post
            _set_player_announced_achievements(player, eligible_current)
            db.add(player)
            db.commit()
            return

        already = set(_player_announced_achievements(player))
        new_unlocks = eligible_current - already
        if not new_unlocks:
            return

        # Persist before posting — a webhook hiccup must not cause re-announcement
        _set_player_announced_achievements(player, already | eligible_current)
        db.add(player)
        db.commit()

        for ach in sorted(new_unlocks):
            post_discord_achievement(player.name, ach, player.club_id, db)
    except Exception:
        pass