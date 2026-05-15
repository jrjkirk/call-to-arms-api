from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select, or_

from database import get_session
from models import Player, LeagueResult, LeagueRating

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_origin_regex=r"https://call-to-arms-web.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/players")
def list_players(session: Session = Depends(get_session)):
    """Return all active players, ordered by name."""
    statement = select(Player).where(Player.active == True).order_by(Player.name)
    players = session.exec(statement).all()
    return players


@app.get("/players/{player_id}")
def get_player(player_id: int, session: Session = Depends(get_session)):
    """Return one player plus their league stats."""

    # 1. Fetch the player itself. 404 if missing.
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    # 2. Fetch their current league rating, if any.
    rating_stmt = select(LeagueRating).where(LeagueRating.player_id == player_id)
    rating = session.exec(rating_stmt).first()

    # 3. Fetch all their league results, most recent first.
    results_stmt = (
        select(LeagueResult)
        .where(
            or_(
                LeagueResult.player_1_id == player_id,
                LeagueResult.player_2_id == player_id,
            )
        )
        .order_by(LeagueResult.created_at.desc())
    )
    results = session.exec(results_stmt).all()

    # 4. Compute simple win/loss/draw counts.
    wins = 0
    losses = 0
    draws = 0
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
        # Any other result string is ignored, defensively.

    # 5. Bundle it all into one response.
    return {
        "player": player,
        "league": {
            "rating": rating.rating if rating else None,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "total_games": len(results),
            "recent_results": results[:10],  # last 10 only, to keep payload small
        },
    }