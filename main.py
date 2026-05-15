from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from database import get_session
from models import Player, LeagueResult, LeagueRating
from services import compute_league_record, fetch_player_results

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
    return session.exec(statement).all()


@app.get("/players/{player_id}")
def get_player(player_id: int, session: Session = Depends(get_session)):
    """Return one player plus their league stats."""
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    rating_stmt = select(LeagueRating).where(LeagueRating.player_id == player_id)
    rating = session.exec(rating_stmt).first()

    results = fetch_player_results(session, player_id)
    record = compute_league_record(player_id, results)

    return {
        "player": player,
        "league": {
            "rating": rating.rating if rating else None,
            **record,
            "recent_results": results[:10],
        },
    }

@app.get("/league/rankings")
def league_rankings(session: Session = Depends(get_session)):
    """Return league standings, ordered by rating descending.
    
    Only includes players who have a rating (i.e. who have played at least one
    league game). For each, includes their current record (W/L/D/total).
    """
    # Pull all ratings, joined to players for the canonical name and faction.
    statement = (
        select(LeagueRating, Player)
        .join(Player, Player.id == LeagueRating.player_id)
        .where(Player.active == True)
        .order_by(LeagueRating.rating.desc())
    )
    rows = session.exec(statement).all()

    rankings = []
    for rank, (rating, player) in enumerate(rows, start=1):
        results = fetch_player_results(session, player.id)
        record = compute_league_record(player.id, results)
        rankings.append({
            "rank": rank,
            "player_id": player.id,
            "name": player.name,
            "default_faction": player.default_faction,
            "rating": rating.rating,
            **record,
        })

    return rankings