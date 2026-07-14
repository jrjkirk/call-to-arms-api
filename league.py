"""League result submission and ELO recalculation.

Ratings are never updated incrementally. Every submission inserts a
LeagueResult row and then runs a full recalculation that replays all
results from scratch, ordered by id ascending.
"""
import os
from collections import defaultdict
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select, or_

from auth import require_user
from database import _default_club_id, get_session, scoped
from models import LeagueRating, LeagueResult, Player, User
from services import announce_new_achievements

router = APIRouter(prefix="/league", tags=["league"])

DISCORD_LEAGUE_RESULT_WEBHOOK_URL = os.environ.get("DISCORD_LEAGUE_RESULT_WEBHOOK_URL", "")

VALID_RESULTS = {"Player 1 Victory", "Player 2 Victory", "Draw"}
VALID_GAME_TYPES = {"Casual", "Competitive"}
VALID_PAINTING = {None, "Partially Painted", "Fully Painted"}
_NONE_SENTINELS = {"— None —", ""}


def _normalise_optional(value: Optional[str]) -> Optional[str]:
    if not value or value in _NONE_SENTINELS:
        return None
    return value


def _painting_bonus(value: Optional[str]) -> float:
    if not value:
        return 0.0
    v = value.strip().lower()
    if v == "fully painted":
        return 3.0
    if v == "partially painted":
        return 1.0
    return 0.0


def _recalculate_ratings(db: Session) -> None:
    """Full replay of all LeagueResult rows. Rebuilds the LeagueRating table from scratch."""
    results = db.exec(select(LeagueResult).order_by(LeagueResult.id)).all()

    ratings: dict[int, float] = {}
    latest_name: dict[int, str] = {}

    for row in results:
        p1 = row.player_1_id
        p2 = row.player_2_id
        if p1 is None or p2 is None or p1 == p2:
            continue

        gt = (row.game_type or "").strip()
        if not gt:
            row.game_type = "Competitive"
            gt = "Competitive"

        k = 10 if gt.lower() == "casual" else 40

        r1 = ratings.get(p1, 1000.0)
        r2 = ratings.get(p2, 1000.0)

        e1 = 1.0 / (1.0 + 10.0 ** ((r2 - r1) / 400.0))
        e2 = 1.0 / (1.0 + 10.0 ** ((r1 - r2) / 400.0))

        if row.result == "Player 1 Victory":
            score = 1.0
        elif row.result == "Player 2 Victory":
            score = 0.0
        else:
            score = 0.5

        new_r1 = r1 + k * (score - e1) + _painting_bonus(row.player_1_painting_bonus)
        new_r2 = r2 + k * ((1.0 - score) - e2) + _painting_bonus(row.player_2_painting_bonus)

        row.player_1_rating_before = r1
        row.player_2_rating_before = r2
        row.player_1_rating_after = new_r1
        row.player_2_rating_after = new_r2
        row.k_factor_used = k
        db.add(row)

        ratings[p1] = new_r1
        ratings[p2] = new_r2
        latest_name[p1] = row.player_1_name
        latest_name[p2] = row.player_2_name

    for old in db.exec(select(LeagueRating)).all():
        db.delete(old)

    club_id = _default_club_id(db)
    now = datetime.utcnow()
    for pid, rating in ratings.items():
        db.add(LeagueRating(
            player_id=pid,
            player_name=latest_name[pid],
            rating=rating,
            updated_at=now,
            club_id=club_id,
        ))


def _post_league_webhook(row: LeagueResult) -> None:
    url = DISCORD_LEAGUE_RESULT_WEBHOOK_URL
    if not url:
        return

    p1_fac = row.player_1_faction or "—"
    p2_fac = row.player_2_faction or "—"
    r1_before = round(row.player_1_rating_before or 0)
    r1_after = round(row.player_1_rating_after or 0)
    r2_before = round(row.player_2_rating_before or 0)
    r2_after = round(row.player_2_rating_after or 0)

    def signed(n: int) -> str:
        return f"+{n}" if n >= 0 else str(n)

    if row.result in ("Player 1 Victory", "Player 2 Victory"):
        header = "⚔️ **A Battle Concludes!** ⚔️"
        winner = row.player_1_name if row.result == "Player 1 Victory" else row.player_2_name
        winner_line = f"🏆 Victor: **{winner}**"
        flavour = "*The standings shift. Who will rise next?*"
    else:
        header = "⚔️ **A Hard-Fought Stalemate** ⚔️"
        winner_line = "🤝 The contest ended in a **draw**."
        flavour = "*Honour preserved on both sides. The contest continues.*"

    elo_block = (
        f"ELO Shift:\n"
        f"• {row.player_1_name}: {signed(r1_after - r1_before)} → **{r1_after}**\n"
        f"• {row.player_2_name}: {signed(r2_after - r2_before)} → **{r2_after}**"
    )

    content = "\n".join([
        header,
        f"**{row.player_1_name}** ({p1_fac}) vs **{row.player_2_name}** ({p2_fac})",
        elo_block,
        winner_line,
        flavour,
    ])

    try:
        httpx.post(url, json={"content": content}, timeout=5.0)
    except Exception:
        pass


@router.get("/factions")
def list_factions(db: Session = Depends(get_session)):
    rows = db.exec(select(LeagueResult)).all()
    factions: set[str] = set()
    for r in rows:
        if r.player_1_faction:
            factions.add(r.player_1_faction)
        if r.player_2_faction:
            factions.add(r.player_2_faction)
    return {"factions": sorted(factions)}


@router.get("/faction-stats")
def faction_stats(
    faction: str = Query(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    rows = db.exec(
        scoped(LeagueResult, user.club_id).where(
            or_(
                LeagueResult.player_1_faction == faction,
                LeagueResult.player_2_faction == faction,
            )
        )
    ).all()

    # player_id -> {name, wins, draws, losses}
    stats: dict[int, dict] = defaultdict(lambda: {"name": "", "wins": 0, "draws": 0, "losses": 0})

    for r in rows:
        p1_used = r.player_1_faction == faction and r.player_1_id is not None
        p2_used = r.player_2_faction == faction and r.player_2_id is not None

        if p1_used:
            entry = stats[r.player_1_id]
            entry["name"] = r.player_1_name
            if r.result == "Player 1 Victory":
                entry["wins"] += 1
            elif r.result == "Player 2 Victory":
                entry["losses"] += 1
            else:
                entry["draws"] += 1

        if p2_used:
            entry = stats[r.player_2_id]
            entry["name"] = r.player_2_name
            if r.result == "Player 2 Victory":
                entry["wins"] += 1
            elif r.result == "Player 1 Victory":
                entry["losses"] += 1
            else:
                entry["draws"] += 1

    players = []
    for player_id, s in stats.items():
        total = s["wins"] + s["draws"] + s["losses"]
        players.append({
            "player_id": player_id,
            "player_name": s["name"],
            "wins": s["wins"],
            "draws": s["draws"],
            "losses": s["losses"],
            "total_games": total,
            "adjusted_win_rate": (s["wins"] + 0.5 * s["draws"] + 2.5) / (total + 5),
        })

    players.sort(key=lambda p: (-p["adjusted_win_rate"], -p["total_games"]))
    return {"faction": faction, "players": players}


class SubmitResultIn(BaseModel):
    player_1_id: int
    player_2_id: int
    player_1_faction: Optional[str] = None
    player_2_faction: Optional[str] = None
    player_1_painting_bonus: Optional[str] = None
    player_2_painting_bonus: Optional[str] = None
    game_type: str
    result: str


@router.post("/results")
def submit_result(
    body: SubmitResultIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    if user.player_id is None:
        raise HTTPException(status_code=400, detail="No linked player profile — claim your profile first.")

    if body.player_1_id == body.player_2_id:
        raise HTTPException(status_code=422, detail="Players must be distinct.")

    p1 = db.get(Player, body.player_1_id)
    if p1 is None or not p1.active or p1.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player 1 not found or inactive.")

    p2 = db.get(Player, body.player_2_id)
    if p2 is None or not p2.active or p2.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player 2 not found or inactive.")

    if body.result not in VALID_RESULTS:
        raise HTTPException(status_code=422, detail=f"Result must be one of: {', '.join(sorted(VALID_RESULTS))}")

    if body.game_type not in VALID_GAME_TYPES:
        raise HTTPException(status_code=422, detail="Game type must be Casual or Competitive.")

    p1_faction = _normalise_optional(body.player_1_faction)
    p2_faction = _normalise_optional(body.player_2_faction)
    p1_painting = _normalise_optional(body.player_1_painting_bonus)
    p2_painting = _normalise_optional(body.player_2_painting_bonus)

    if p1_painting not in VALID_PAINTING:
        raise HTTPException(status_code=422, detail="Invalid player 1 painting bonus.")
    if p2_painting not in VALID_PAINTING:
        raise HTTPException(status_code=422, detail="Invalid player 2 painting bonus.")

    result_date = datetime.utcnow().strftime("%d/%m/%Y")

    # Duplicate guard: match on every field. NULL == NULL must be handled explicitly
    # because SQL NULL comparisons use IS NULL, not =.
    dup_query = (
        scoped(LeagueResult, user.club_id)
        .where(LeagueResult.player_1_id == body.player_1_id)
        .where(LeagueResult.player_2_id == body.player_2_id)
        .where(LeagueResult.result == body.result)
        .where(LeagueResult.result_date == result_date)
        .where(LeagueResult.game_type == body.game_type)
    )
    if p1_faction is None:
        dup_query = dup_query.where(LeagueResult.player_1_faction.is_(None))
    else:
        dup_query = dup_query.where(LeagueResult.player_1_faction == p1_faction)
    if p2_faction is None:
        dup_query = dup_query.where(LeagueResult.player_2_faction.is_(None))
    else:
        dup_query = dup_query.where(LeagueResult.player_2_faction == p2_faction)
    if p1_painting is None:
        dup_query = dup_query.where(LeagueResult.player_1_painting_bonus.is_(None))
    else:
        dup_query = dup_query.where(LeagueResult.player_1_painting_bonus == p1_painting)
    if p2_painting is None:
        dup_query = dup_query.where(LeagueResult.player_2_painting_bonus.is_(None))
    else:
        dup_query = dup_query.where(LeagueResult.player_2_painting_bonus == p2_painting)

    if db.exec(dup_query).first() is not None:
        return {"ok": True, "duplicate": True}

    row = LeagueResult(
        player_1_id=body.player_1_id,
        player_1_name=p1.name,
        player_2_id=body.player_2_id,
        player_2_name=p2.name,
        result=body.result,
        result_date=result_date,
        player_1_faction=p1_faction,
        player_2_faction=p2_faction,
        player_1_painting_bonus=p1_painting,
        player_2_painting_bonus=p2_painting,
        game_type=body.game_type,
        club_id=user.club_id,
    )
    db.add(row)
    db.flush()  # assign row.id within the transaction so the recalc includes this row

    _recalculate_ratings(db)
    db.commit()  # single commit for insert + full recalc
    db.refresh(row)

    _post_league_webhook(row)
    announce_new_achievements(db, row.player_1_id)
    announce_new_achievements(db, row.player_2_id)

    return {"ok": True, "duplicate": False, "result": row}
