from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select

from database import get_session
from models import Player, LeagueResult, LeagueRating, Signup, Pairing, PublishState
from services import (
    compute_league_record,
    fetch_player_results,
    fetch_player_signups,
    signup_counts_per_system,
    faction_usage_per_system,
    build_elo_history,
    first_league_winner_id,
    compute_achievements,
    player_titles,
    ACHIEVEMENT_DESCRIPTIONS,
)
from auth import router as auth_router
from signups import router as signups_router
from league import router as league_router
from admin import router as admin_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "https://call-to-arms-web.vercel.app",
    ],
    allow_origin_regex=r"https://call-to-arms-web.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(signups_router)
app.include_router(league_router)
app.include_router(admin_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/players")
def list_players(session: Session = Depends(get_session)):
    statement = select(Player).where(Player.active == True).order_by(Player.name)
    return session.exec(statement).all()


@app.get("/players/{player_id}")
def get_player(player_id: int, session: Session = Depends(get_session)):
    player = session.get(Player, player_id)
    if player is None:
        raise HTTPException(status_code=404, detail="Player not found")

    rating_row = session.exec(
        select(LeagueRating).where(LeagueRating.player_id == player_id)
    ).first()
    results = fetch_player_results(session, player_id)
    record = compute_league_record(player_id, results)
    elo_history = build_elo_history(player_id, results)

    rank = None
    if rating_row is not None:
        higher = session.exec(
            select(LeagueRating).where(LeagueRating.rating > rating_row.rating)
        ).all()
        rank = len(higher) + 1

    signups = fetch_player_signups(session, player_id)
    sign_counts = signup_counts_per_system(signups)
    fac_usage = faction_usage_per_system(signups)

    first_winner = first_league_winner_id(session)
    achievements = compute_achievements(
        player_id, record, results, fac_usage, elo_history, first_winner
    )
    achievements_detailed = [
        {"name": a, "description": ACHIEVEMENT_DESCRIPTIONS.get(a, "")}
        for a in achievements
    ]

    recent = results[:5]

    return {
        "player": player,
        "titles": player_titles(player),
        "achievements": achievements_detailed,
        "signup_counts": sign_counts,
        "faction_usage": fac_usage,
        "league": {
            "rating": rating_row.rating if rating_row else None,
            "rank": rank,
            **record,
            "recent_results": recent,
            "elo_history": elo_history,
        },
    }


@app.get("/league/rankings")
def league_rankings(session: Session = Depends(get_session)):
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

        faction_counts: dict[str, int] = {}
        for r in results:
            f = r.player_1_faction if r.player_1_id == player.id else r.player_2_faction
            if f:
                faction_counts[f] = faction_counts.get(f, 0) + 1
        most_played = max(faction_counts, key=faction_counts.get) if faction_counts else None

        rankings.append({
            "rank": rank,
            "player_id": player.id,
            "name": player.name,
            "default_faction": player.default_faction,
            "most_played_faction": most_played,
            "rating": rating.rating,
            **record,
        })

    return rankings


@app.get("/signups/stats")
def signups_stats(system: str, week: str, session: Session = Depends(get_session)):
    rows = session.exec(
        select(Signup)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at.desc())
    ).all()

    latest_by_player: dict = {}
    for s in rows:
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


def _public_vibe_display(a_vibe, b_vibe):
    if a_vibe and b_vibe:
        if a_vibe.strip().lower() == b_vibe.strip().lower():
            return a_vibe
        return None
    return a_vibe or b_vibe or None


@app.get("/pairings")
def get_pairings(system: str, week: str, session: Session = Depends(get_session)):
    gate = session.exec(
        select(PublishState).where(
            (PublishState.week == week) & (PublishState.system == system)
        )
    ).first()

    if not gate or not gate.published:
        return {"published": False, "system": system, "week": week, "matchups": []}

    prs = session.exec(
        select(Pairing)
        .where((Pairing.week == week) & (Pairing.system == system))
        .order_by(Pairing.id)
    ).all()

    signup_ids = {p.a_signup_id for p in prs} | {p.b_signup_id for p in prs if p.b_signup_id}
    signups_by_id: dict[int, Signup] = {}
    if signup_ids:
        rows = session.exec(select(Signup).where(Signup.id.in_(signup_ids))).all()
        signups_by_id = {s.id: s for s in rows}

    matchups = []
    for p in prs:
        a = signups_by_id.get(p.a_signup_id)
        b = signups_by_id.get(p.b_signup_id) if p.b_signup_id else None

        game_type = _public_vibe_display(
            a.vibe if a else None,
            b.vibe if b else None,
        )

        a_pts = a.points if a else None
        b_pts = b.points if b else None
        if a_pts and b_pts:
            points = str(min(a_pts, b_pts)) if a_pts == b_pts else f"{min(a_pts, b_pts)}-{max(a_pts, b_pts)}"
        else:
            points = str(a_pts or b_pts) if (a_pts or b_pts) else None

        a_eta = a.eta if a else None
        b_eta = b.eta if b else None
        if a_eta and b_eta:
            eta = max(a_eta, b_eta)
        else:
            eta = a_eta or b_eta

        is_bye = b is None

        matchups.append({
            "id": p.id,
            "player_a_name": a.player_name if a else f"A#{p.a_signup_id}",
            "player_a_faction": p.a_faction or (a.faction if a else None),
            "player_b_name": b.player_name if b else None,
            "player_b_faction": p.b_faction or (b.faction if b else None) if b else None,
            "is_bye": is_bye,
            "game_type": game_type,
            "eta": eta,
            "points": points,
            "prearranged": p.prearranged,
        })

    return {
        "published": True,
        "system": system,
        "week": week,
        "matchups": matchups,
        "total_players": len({p.a_signup_id for p in prs} | {p.b_signup_id for p in prs if p.b_signup_id}),
        "total_matchups": len([m for m in matchups if not m["is_bye"]]),
        "byes": len([m for m in matchups if m["is_bye"]]),
    }