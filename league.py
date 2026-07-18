"""League result submission and ELO recalculation.

Ratings are never updated incrementally. Every submission inserts a
LeagueResult row and then runs a full recalculation that replays all
results from scratch, ordered by id ascending.
"""
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, or_, select

from auth import require_user, current_user
from database import get_session, resolve_request_club_id, resolve_webhook_url, scoped
from models import ClubSystem, LeagueConfig, LeagueRating, LeagueResult, LeagueSeason, Player, SystemConfig, User
from services import announce_new_achievements

router = APIRouter(prefix="/league", tags=["league"])

VALID_RESULTS = {"Player 1 Victory", "Player 2 Victory", "Draw"}
VALID_GAME_TYPES = {"Casual", "Competitive"}
VALID_PAINTING = {None, "Partially Painted", "Fully Painted"}
_NONE_SENTINELS = {"— None —", ""}


def _normalise_optional(value: Optional[str]) -> Optional[str]:
    if not value or value in _NONE_SENTINELS:
        return None
    return value


def _get_league_config(db: Session, club_id: int, system_id: int) -> LeagueConfig:
    """This (club, system) league's scoring config, or an unsaved defaults
    instance (which reproduces the original hardcoded ELO exactly)."""
    cfg = db.exec(
        select(LeagueConfig).where(
            LeagueConfig.club_id == club_id, LeagueConfig.system_id == system_id
        )
    ).first()
    return cfg or LeagueConfig(club_id=club_id, system_id=system_id)


def _painting_bonus(cfg: LeagueConfig, value: Optional[str]) -> float:
    if not value:
        return 0.0
    v = value.strip().lower()
    if v == "fully painted":
        return cfg.painting_fully_bonus
    if v == "partially painted":
        return cfg.painting_partial_bonus
    return 0.0


def _resolve_league_system_id(db: Session, club_id: int) -> Optional[int]:
    """The system this club runs its league for. Single-league today (the one
    ClubSystem row with league_enabled); the explicit per-system system param
    arrives in a later phase."""
    row = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == club_id, ClubSystem.league_enabled == True
        )
    ).first()
    return row.system_id if row else None


def _resolve_system_id(db: Session, club_id: int, system: Optional[str]) -> Optional[int]:
    """Resolve which system's league a request is for.

    `system` given: must be a real catalogue system with a league enabled for
    this club, else 422/404. `system` omitted: falls back to
    _resolve_league_system_id (today's "the club's one league-enabled
    system" behaviour) — every existing caller that doesn't pass `system`
    keeps working unchanged, including once a club runs more than one
    league (it then just needs to start passing `system` explicitly)."""
    if system is None:
        return _resolve_league_system_id(db, club_id)
    config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == club_id,
            ClubSystem.system_id == config.id,
            ClubSystem.league_enabled == True,
        )
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail=f"No league enabled for {system!r} at this club.")
    return config.id


def _current_season_id(db: Session, club_id: int, system_id: int) -> Optional[int]:
    """The season whose date range contains today for this (club, system);
    failing that, the most recent season by start_date; None if none exist."""
    seasons = db.exec(
        select(LeagueSeason)
        .where(LeagueSeason.club_id == club_id, LeagueSeason.system_id == system_id)
        .order_by(LeagueSeason.start_date.desc())
    ).all()
    today = date.today()
    for s in seasons:
        if s.start_date <= today and (s.end_date is None or today <= s.end_date):
            return s.id
    return seasons[0].id if seasons else None


def _apply_elo(cfg: LeagueConfig, r1: float, r2: float, row: LeagueResult) -> tuple[float, float, Optional[int]]:
    gt = (row.game_type or "Competitive").lower()
    k = cfg.k_casual if gt == "casual" else cfg.k_competitive

    e1 = 1.0 / (1.0 + 10.0 ** ((r2 - r1) / 400.0))
    e2 = 1.0 / (1.0 + 10.0 ** ((r1 - r2) / 400.0))

    if row.result == "Player 1 Victory":
        score = 1.0
    elif row.result == "Player 2 Victory":
        score = 0.0
    else:
        score = 0.5

    new_r1 = r1 + k * (score - e1) + _painting_bonus(cfg, row.player_1_painting_bonus)
    new_r2 = r2 + k * ((1.0 - score) - e2) + _painting_bonus(cfg, row.player_2_painting_bonus)
    return new_r1, new_r2, k


def _apply_winloss(cfg: LeagueConfig, r1: float, r2: float, row: LeagueResult) -> tuple[float, float, Optional[int]]:
    """Flat points: win/draw/loss points per LeagueConfig, cumulative from the
    starting rating. Painting bonuses only apply if winloss_use_painting."""
    if row.result == "Player 1 Victory":
        d1, d2 = cfg.points_win, cfg.points_loss
    elif row.result == "Player 2 Victory":
        d1, d2 = cfg.points_loss, cfg.points_win
    else:
        d1 = d2 = cfg.points_draw

    if cfg.winloss_use_painting:
        d1 += _painting_bonus(cfg, row.player_1_painting_bonus)
        d2 += _painting_bonus(cfg, row.player_2_painting_bonus)

    return r1 + d1, r2 + d2, None


# Method name -> (cfg, r1, r2, row) -> (new_r1, new_r2, k_used). Add a new
# entry here (e.g. "bayesian") to plug in a third scoring method — nothing
# else in _recalculate_ratings needs to change.
_SCORING_METHODS = {
    "elo": _apply_elo,
    "winloss": _apply_winloss,
}


def _recalculate_ratings(db: Session, club_id: int, system_id: int, season_id: int) -> None:
    """Full replay of one (club, system, season)'s LeagueResult rows, rebuilding
    that season's LeagueRating rows from scratch (ordered by id ascending).

    Scoring comes from that league's LeagueConfig.scoring_method. With the
    default config ("elo") this reproduces the original hardcoded ELO
    byte-for-byte (K 10/40, +3/+1, start 1000)."""
    cfg = _get_league_config(db, club_id, system_id)
    start = cfg.starting_rating
    apply_result = _SCORING_METHODS.get(cfg.scoring_method, _apply_elo)

    results = db.exec(
        select(LeagueResult)
        .where(LeagueResult.club_id == club_id)
        .where(LeagueResult.system_id == system_id)
        .where(LeagueResult.season_id == season_id)
        .order_by(LeagueResult.id)
    ).all()

    ratings: dict[int, float] = {}
    latest_name: dict[int, str] = {}

    for row in results:
        p1 = row.player_1_id
        p2 = row.player_2_id
        if p1 is None or p2 is None or p1 == p2:
            continue

        if not (row.game_type or "").strip():
            row.game_type = "Competitive"

        r1 = ratings.get(p1, start)
        r2 = ratings.get(p2, start)

        new_r1, new_r2, k_used = apply_result(cfg, r1, r2, row)

        row.player_1_rating_before = r1
        row.player_2_rating_before = r2
        row.player_1_rating_after = new_r1
        row.player_2_rating_after = new_r2
        row.k_factor_used = k_used
        db.add(row)

        ratings[p1] = new_r1
        ratings[p2] = new_r2
        latest_name[p1] = row.player_1_name
        latest_name[p2] = row.player_2_name

    # Rebuild only THIS (club, system, season)'s ratings — other systems'/
    # seasons' ratings are untouched.
    old_ratings = db.exec(
        select(LeagueRating)
        .where(LeagueRating.club_id == club_id)
        .where(LeagueRating.system_id == system_id)
        .where(LeagueRating.season_id == season_id)
    ).all()
    for old in old_ratings:
        db.delete(old)

    now = datetime.utcnow()
    for pid, rating in ratings.items():
        db.add(LeagueRating(
            player_id=pid,
            player_name=latest_name[pid],
            rating=rating,
            updated_at=now,
            club_id=club_id,
            system_id=system_id,
            season_id=season_id,
        ))


def _post_league_webhook(db: Session, row: LeagueResult) -> None:
    url = resolve_webhook_url(db, row.club_id, "league_result")
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
def list_factions(
    club: str | None = None,
    system: str | None = None,
    origin: str | None = Header(default=None),
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Optional-auth. A logged-in caller is always scoped to their own club
    (user.club_id) and the `club`/Origin are ignored — this closes the leak
    where a logged-in user on the bare/default hostname (which resolves to
    "manchester") was served Manchester's faction list. Genuinely anonymous
    requests resolve via an explicit `club` param first, then the request's
    Origin header (subdomain-based resolution, no param needed for real
    browser calls); with neither, an anonymous request falls back to the
    fail-loud single-active-club stopgap. See resolve_request_club_id.

    `system` selects which system's league (omit for the club's one
    league-enabled system, today's behaviour — see _resolve_system_id)."""
    try:
        club_id = resolve_request_club_id(db, user, club, origin)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    system_id = _resolve_system_id(db, club_id, system)
    if system_id is None:
        return {"factions": []}

    rows = db.exec(scoped(LeagueResult, club_id).where(LeagueResult.system_id == system_id)).all()
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
    system: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    system_id = _resolve_system_id(db, user.club_id, system)
    if system_id is None:
        return {"faction": faction, "players": []}

    rows = db.exec(
        scoped(LeagueResult, user.club_id)
        .where(LeagueResult.system_id == system_id)
        .where(
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
    # Which system's league this result is for. Omit for the club's one
    # league-enabled system (today's single-league behaviour).
    system: Optional[str] = None


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

    # Resolve which system's league this result belongs to, and its current
    # season.
    system_id = _resolve_system_id(db, user.club_id, body.system)
    if system_id is None:
        raise HTTPException(status_code=422, detail="No league is enabled for this club.")
    season_id = _current_season_id(db, user.club_id, system_id)
    if season_id is None:
        raise HTTPException(status_code=422, detail="No league season is configured.")

    result_date = datetime.utcnow().strftime("%d/%m/%Y")

    # Duplicate guard: match on every field. NULL == NULL must be handled explicitly
    # because SQL NULL comparisons use IS NULL, not =.
    dup_query = (
        scoped(LeagueResult, user.club_id)
        .where(LeagueResult.system_id == system_id)
        .where(LeagueResult.season_id == season_id)
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
        system_id=system_id,
        season_id=season_id,
    )
    db.add(row)
    db.flush()  # assign row.id within the transaction so the recalc includes this row

    _recalculate_ratings(db, user.club_id, system_id, season_id)
    db.commit()  # single commit for insert + full recalc
    db.refresh(row)

    _post_league_webhook(db, row)
    announce_new_achievements(db, row.player_1_id, system_id)
    announce_new_achievements(db, row.player_2_id, system_id)

    return {"ok": True, "duplicate": False, "result": row}
