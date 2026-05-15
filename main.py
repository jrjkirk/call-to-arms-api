from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from database import get_session
from models import Player, LeagueResult, LeagueRating, Signup
from services import compute_league_record, fetch_player_results

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
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
    """Return one player plus their league stats and rank."""
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    rating_stmt = select(LeagueRating).where(LeagueRating.player_id == player_id)
    rating = session.exec(rating_stmt).first()

    # Rank: how does this player's rating compare?
    rank = None
    if rating is not None:
        higher_count_stmt = select(LeagueRating).where(LeagueRating.rating > rating.rating)
        higher = session.exec(higher_count_stmt).all()
        rank = len(higher) + 1

    results = fetch_player_results(session, player_id)
    record = compute_league_record(player_id, results)

    return {
        "player": player,
        "league": {
            "rating": rating.rating if rating else None,
            "rank": rank,
            **record,
            "recent_results": results[:10],
        },
    }


@app.get("/league/rankings")
def league_rankings(session: Session = Depends(get_session)):
    """Return league standings, ordered by rating descending."""
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

        # Figure out the player's most-played faction across league results
        faction_counts: dict[str, int] = {}
        for r in results:
            faction = r.player_1_faction if r.player_1_id == player.id else r.player_2_faction
            if faction:
                faction_counts[faction] = faction_counts.get(faction, 0) + 1
        most_played_faction = max(faction_counts, key=faction_counts.get) if faction_counts else None

        rankings.append({
            "rank": rank,
            "player_id": player.id,
            "name": player.name,
            "default_faction": player.default_faction,
            "most_played_faction": most_played_faction,
            "rating": rating.rating,
            **record,
        })

    return rankings


@app.get("/signups/stats")
def signups_stats(system: str, week: str, session: Session = Depends(get_session)):
    """Counts for the Call to Arms tab: total signed up, newcomers, veterans for the given week+system."""
    # Latest signup per player_id wins (a player may have edited their signup multiple times).
    rows_stmt = (
        select(Signup)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at.desc())
    )
    all_rows = session.exec(rows_stmt).all()

    latest_by_player: dict[int | None, Signup] = {}
    for s in all_rows:
        key = s.player_id if s.player_id is not None else id(s)
        if key not in latest_by_player:
            latest_by_player[key] = s

    signups = list(latest_by_player.values())

    total = len(signups)
    newcomers = sum(1 for s in signups if (s.experience or "").lower().startswith("new"))
    veterans = sum(1 for s in signups if (s.experience or "").lower().startswith("vet"))

    return {
        "system": system,
        "week": week,
        "signed_up": total,
        "newcomers": newcomers,
        "veterans": veterans,
    }