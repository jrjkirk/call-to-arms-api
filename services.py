"""Shared business logic, independent of the API layer.

Functions here compute things from data. They take inputs (usually a session and
some IDs), return outputs (usually dicts or simple types). They don't know about
FastAPI, requests, responses, or HTTP. This separation matters because: (a) it's
easier to test, (b) the same logic gets re-used across endpoints without duplication,
(c) when we eventually replace the API layer or add a CLI, the logic doesn't move.
"""
from typing import Iterable
from sqlmodel import Session, select, or_

from models import LeagueResult


def compute_league_record(player_id: int, results: Iterable[LeagueResult]) -> dict:
    """Count wins, losses, and draws for a player across the given league results.

    Result strings stored as 'Player 1 Victory', 'Player 2 Victory', or 'Draw',
    from the perspective of player_1 on each row. We flip if the player is player_2.
    """
    wins = losses = draws = 0
    for r in results:
        is_player_1 = r.player_1_id == player_id
        if r.result == "Draw":
            draws += 1
        elif r.result == "Player 1 Victory":
            if is_player_1:
                wins += 1
            else:
                losses += 1
        elif r.result == "Player 2 Victory":
            if is_player_1:
                losses += 1
            else:
                wins += 1
        # Other result strings are ignored, defensively.
    return {"wins": wins, "losses": losses, "draws": draws, "total_games": wins + losses + draws}


def fetch_player_results(session: Session, player_id: int) -> list[LeagueResult]:
    """Fetch all league results involving the given player, newest first."""
    statement = (
        select(LeagueResult)
        .where(
            or_(
                LeagueResult.player_1_id == player_id,
                LeagueResult.player_2_id == player_id,
            )
        )
        .order_by(LeagueResult.created_at.desc())
    )
    return session.exec(statement).all()