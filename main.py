from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select, or_

from database import get_session
from models import Player, LeagueResult, LeagueRating, Signup, Pairing, PublishState, User
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
from auth import router as auth_router, require_user
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
        "https://www.calltoarms.app",
        "https://calltoarms.app",
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
def list_players(_: User = Depends(require_user), session: Session = Depends(get_session)):
    players = session.exec(
        select(Player).where(Player.active == True).order_by(Player.name)
    ).all()

    signup_rows = session.exec(
        select(Signup.player_id, Signup.system)
        .where(Signup.player_id.isnot(None))
        .distinct()
    ).all()

    systems_by_player: dict[int, set] = {}
    for player_id, system in signup_rows:
        systems_by_player.setdefault(player_id, set()).add(system)

    result = []
    for player in players:
        player_dict = player.model_dump()
        player_dict["systems_played"] = sorted(systems_by_player.get(player.id, set()))
        result.append(player_dict)
    return result


def _parse_week(week: str) -> datetime:
    try:
        return datetime.strptime(week, "%d/%m/%Y")
    except ValueError:
        return datetime.min


@app.get("/players/{player_id}")
def get_player(player_id: int, _: User = Depends(require_user), session: Session = Depends(get_session)):
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

    # Discord identity
    user = session.exec(select(User).where(User.player_id == player_id)).first()
    discord_info = (
        {"discord_name": user.discord_name, "avatar_url": user.avatar_url}
        if user else None
    )

    # Build lookup for TOW league results: (frozenset{p1_id, p2_id}, date) -> LeagueResult
    tow_lookup: dict[tuple, LeagueResult] = {}
    for r in results:
        if r.result_date and r.player_1_id and r.player_2_id:
            parsed_date = None
            for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
                try:
                    parsed_date = datetime.strptime(r.result_date, fmt).date()
                    break
                except ValueError:
                    continue
            if parsed_date:
                tow_lookup[(frozenset({r.player_1_id, r.player_2_id}), parsed_date)] = r

    # Index this player's signups by system and by id for quick lookup
    signup_ids_by_system: dict[str, set[int]] = {}
    signup_by_id: dict[int, Signup] = {}
    for s in signups:
        if s.id is not None:
            signup_ids_by_system.setdefault(s.system, set()).add(s.id)
            signup_by_id[s.id] = s

    SYSTEMS = ["The Old World", "The Horus Heresy", "Kill Team"]
    recent_games_by_system: dict[str, list] = {}

    for system in SYSTEMS:
        sys_signup_ids = signup_ids_by_system.get(system, set())
        if not sys_signup_ids:
            recent_games_by_system[system] = []
            continue

        pairings = session.exec(
            select(Pairing)
            .where(Pairing.system == system)
            .where(
                or_(
                    Pairing.a_signup_id.in_(sys_signup_ids),
                    Pairing.b_signup_id.in_(sys_signup_ids),
                )
            )
        ).all()

        if not pairings:
            recent_games_by_system[system] = []
            continue

        sorted_pairings = sorted(pairings, key=lambda p: _parse_week(p.week), reverse=True)[:5]

        # Fetch opponent signup rows not already cached
        needed = (
            {p.a_signup_id for p in sorted_pairings}
            | {p.b_signup_id for p in sorted_pairings if p.b_signup_id}
        )
        missing = needed - set(signup_by_id.keys())
        if missing:
            for s in session.exec(select(Signup).where(Signup.id.in_(missing))).all():
                signup_by_id[s.id] = s

        games = []
        for p in sorted_pairings:
            a_signup = signup_by_id.get(p.a_signup_id)
            b_signup = signup_by_id.get(p.b_signup_id) if p.b_signup_id else None
            is_bye = p.b_signup_id is None
            a_is_me = p.a_signup_id in sys_signup_ids

            if a_is_me:
                my_faction = p.a_faction or (a_signup.faction if a_signup else None)
                opp_signup = b_signup
                opp_faction = None if is_bye else (p.b_faction or (b_signup.faction if b_signup else None))
            else:
                my_faction = p.b_faction or (b_signup.faction if b_signup else None)
                opp_signup = a_signup
                opp_faction = p.a_faction or (a_signup.faction if a_signup else None)

            opp_name = None if is_bye else (opp_signup.player_name if opp_signup else None)
            opp_player_id = opp_signup.player_id if opp_signup else None

            result_str = None
            if system == "The Old World" and opp_player_id is not None:
                week_date = _parse_week(p.week)
                if week_date is not datetime.min:
                    lr = tow_lookup.get((frozenset({player_id, opp_player_id}), week_date.date()))
                    if lr:
                        is_p1 = lr.player_1_id == player_id
                        if lr.result == "Draw":
                            result_str = "Draw"
                        elif lr.result == "Player 1 Victory":
                            result_str = "Win" if is_p1 else "Loss"
                        elif lr.result == "Player 2 Victory":
                            result_str = "Win" if not is_p1 else "Loss"

            games.append({
                "week": p.week,
                "your_faction": my_faction,
                "opponent_name": opp_name,
                "opponent_faction": opp_faction,
                "result": result_str,
            })

        recent_games_by_system[system] = games

    return {
        "player": player,
        "discord": discord_info,
        "titles": player_titles(player),
        "achievements": achievements_detailed,
        "signup_counts": sign_counts,
        "faction_usage": fac_usage,
        "recent_games_by_system": recent_games_by_system,
        "league": {
            "rating": rating_row.rating if rating_row else None,
            "rank": rank,
            **record,
            "recent_results": recent,
            "elo_history": elo_history,
        },
    }


@app.get("/league/rankings")
def league_rankings(_: User = Depends(require_user), session: Session = Depends(get_session)):
    statement = (
        select(LeagueRating, Player)
        .join(Player, Player.id == LeagueRating.player_id)
        .where(Player.active == True)
        .order_by(LeagueRating.rating.desc())
    )
    rows = session.exec(statement).all()

    # Build previous_rank using a 7-day rolling comparison.
    # For each player, find their earliest-processed result in the last 7 days
    # and use their rating_before from that row as the comparison point.
    cutoff = datetime.utcnow().date() - timedelta(days=7)
    earliest_recent: dict[int, LeagueResult] = {}
    for r in session.exec(select(LeagueResult)).all():
        if not r.result_date:
            continue
        parsed = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(r.result_date, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None or parsed < cutoff:
            continue
        for pid in (r.player_1_id, r.player_2_id):
            if pid is None:
                continue
            if pid not in earliest_recent or r.id < earliest_recent[pid].id:
                earliest_recent[pid] = r

    def _comparison_rating(rating: LeagueRating, player: Player) -> float:
        r = earliest_recent.get(player.id)
        if r is None:
            return rating.rating
        before = r.player_1_rating_before if r.player_1_id == player.id else r.player_2_rating_before
        return before if before is not None else rating.rating

    sorted_by_prev = sorted(rows, key=lambda x: _comparison_rating(x[0], x[1]), reverse=True)
    previous_rank_map = {player.id: i for i, (_, player) in enumerate(sorted_by_prev, start=1)}

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
            "previous_rank": previous_rank_map[player.id],
            "player_id": player.id,
            "name": player.name,
            "default_faction": player.default_faction,
            "most_played_faction": most_played,
            "rating": rating.rating,
            **record,
        })

    return rankings


@app.get("/signups/stats")
def signups_stats(system: str, week: str, _: User = Depends(require_user), session: Session = Depends(get_session)):
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
    av = (a_vibe or "").strip()
    bv = (b_vibe or "").strip()
    av_l = av.lower()
    bv_l = bv.lower()
    if av_l == "intro" or bv_l == "intro":
        return "Intro"
    if av_l == "either" and bv_l == "either":
        return "Either"
    if av_l == "either" and bv:
        return bv
    if bv_l == "either" and av:
        return av
    return av or bv or None


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

        pts_vals = [v for v in (a.points if a else None, b.points if b else None) if isinstance(v, int)]
        points = str(min(pts_vals)) if pts_vals else None

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