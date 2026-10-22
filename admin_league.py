"""Admin endpoints for a system's league: results (with the ratings replay),
logging results from published pairings, scoring config and seasons.
Split out of admin.py; shared helpers still live there.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    get_session,
    require_admin,
)
from database import scoped
from league import (
    VALID_GAME_TYPES,
    VALID_PAINTING,
    VALID_RESULTS,
    _current_season_id,
    _get_league_config,
    find_recent_result,
    _normalise_optional,
    _recalculate_ratings,
    _resolve_system_id,
)
from models import ClubSystem, LeagueConfig, LeagueResult, Pairing, Player, Signup, SystemConfig, User
from signups import (
    _get_system_config,
)

from admin import (
    _require_system_scope,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_require_any_admin = require_admin


# ---------------------------------------------------------------------------
# League result admin — read/edit/delete with full ratings replay
# ---------------------------------------------------------------------------

def _league_result_row(r: LeagueResult) -> dict:
    return {
        "id": r.id,
        "result_date": r.result_date,
        "player_1_id": r.player_1_id,
        "player_1_name": r.player_1_name,
        "player_1_faction": r.player_1_faction,
        "player_1_painting_bonus": r.player_1_painting_bonus,
        "player_1_rating_before": r.player_1_rating_before,
        "player_1_rating_after": r.player_1_rating_after,
        "player_2_id": r.player_2_id,
        "player_2_name": r.player_2_name,
        "player_2_faction": r.player_2_faction,
        "player_2_painting_bonus": r.player_2_painting_bonus,
        "player_2_rating_before": r.player_2_rating_before,
        "player_2_rating_after": r.player_2_rating_after,
        "game_type": r.game_type,
        "result": r.result,
        "k_factor_used": r.k_factor_used,
    }


@router.get("/league/results")
def admin_league_results(
    system: str,
    season_id: Optional[int] = None,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """One system's league results, newest first (display order; recalc
    internally is id-ascending). Defaults to the current season; pass
    season_id to browse an archived one (Phase 5)."""
    _require_system_scope(system, user, db)
    system_id = _resolve_system_id(db, user.club_id, system)
    if system_id is None:
        return []
    resolved_season_id = season_id or _current_season_id(db, user.club_id, system_id)
    if resolved_season_id is None:
        return []
    rows = db.exec(
        scoped(LeagueResult, user.club_id)
        .where(LeagueResult.system_id == system_id)
        .where(LeagueResult.season_id == resolved_season_id)
        .order_by(LeagueResult.id.desc())
    ).all()
    return [_league_result_row(r) for r in rows]


class AdminLeagueResultPatch(BaseModel):
    """Partial update for a LeagueResult row.

    Fields result_date, player_1_rating_before, player_1_rating_after,
    player_2_rating_before, player_2_rating_after, and k_factor_used are
    intentionally absent — Pydantic silently ignores them if sent, since they
    are computed outputs of _recalculate_ratings, not editable inputs.
    """
    player_1_id: Optional[int] = None
    player_2_id: Optional[int] = None
    player_1_faction: Optional[str] = None
    player_2_faction: Optional[str] = None
    player_1_painting_bonus: Optional[str] = None
    player_2_painting_bonus: Optional[str] = None
    game_type: Optional[str] = None
    result: Optional[str] = None


@router.patch("/league/results/{result_id}")
def admin_league_result_patch(
    result_id: int,
    body: AdminLeagueResultPatch,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Partial update of a league result, followed by a full ratings replay."""
    row = db.get(LeagueResult, result_id)
    if row is None or row.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="League result not found.")
    row_config = db.get(SystemConfig, row.system_id)
    _require_system_scope(row_config.legacy_system_name, user, db)

    provided = body.model_fields_set

    if "player_1_id" in provided:
        p1 = db.get(Player, body.player_1_id)
        if p1 is None or not p1.active or p1.club_id != user.club_id:
            raise HTTPException(status_code=404, detail="Player 1 not found or inactive.")
        row.player_1_id = p1.id
        row.player_1_name = p1.name

    if "player_2_id" in provided:
        p2 = db.get(Player, body.player_2_id)
        if p2 is None or not p2.active or p2.club_id != user.club_id:
            raise HTTPException(status_code=404, detail="Player 2 not found or inactive.")
        row.player_2_id = p2.id
        row.player_2_name = p2.name

    if "player_1_faction" in provided:
        row.player_1_faction = _normalise_optional(body.player_1_faction)

    if "player_2_faction" in provided:
        row.player_2_faction = _normalise_optional(body.player_2_faction)

    if "player_1_painting_bonus" in provided:
        val = _normalise_optional(body.player_1_painting_bonus)
        if val not in VALID_PAINTING:
            raise HTTPException(status_code=422, detail="Invalid player 1 painting bonus.")
        row.player_1_painting_bonus = val

    if "player_2_painting_bonus" in provided:
        val = _normalise_optional(body.player_2_painting_bonus)
        if val not in VALID_PAINTING:
            raise HTTPException(status_code=422, detail="Invalid player 2 painting bonus.")
        row.player_2_painting_bonus = val

    if "game_type" in provided:
        if body.game_type not in VALID_GAME_TYPES:
            raise HTTPException(status_code=422, detail="Game type must be Casual or Competitive.")
        row.game_type = body.game_type

    if "result" in provided:
        if body.result not in VALID_RESULTS:
            raise HTTPException(status_code=422, detail=f"Result must be one of: {', '.join(sorted(VALID_RESULTS))}")
        row.result = body.result

    if row.player_1_id == row.player_2_id:
        raise HTTPException(status_code=422, detail="Players must be distinct.")

    db.add(row)
    db.flush()
    _recalculate_ratings(db, user.club_id, row.system_id, row.season_id)
    db.commit()
    db.refresh(row)
    return _league_result_row(row)


@router.delete("/league/results/{result_id}")
def admin_league_result_delete(
    result_id: int,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Delete a league result and replay ratings from scratch."""
    row = db.get(LeagueResult, result_id)
    if row is None or row.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="League result not found.")
    row_config = db.get(SystemConfig, row.system_id)
    _require_system_scope(row_config.legacy_system_name, user, db)

    system_id, season_id = row.system_id, row.season_id
    db.delete(row)
    db.flush()
    _recalculate_ratings(db, user.club_id, system_id, season_id)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Log league results straight from a week's published pairings — the pairings
# already hold both players + factions, so the admin only supplies the outcome
# (win/draw/loss + optional painting) instead of re-typing every field one game
# at a time. Admin-scoped (unlike the player-facing POST /league/results, this
# does NOT require the caller be a linked player). Per-result webhooks and
# achievement announcements are deliberately skipped here — a bulk log
# shouldn't fire N separate Discord posts.
# ---------------------------------------------------------------------------

class FromPairingsResultItem(BaseModel):
    pairing_id: int
    result: str                                  # a VALID_RESULTS value
    player_1_painting_bonus: Optional[str] = None
    player_2_painting_bonus: Optional[str] = None
    game_type: str = "Competitive"


class FromPairingsResultsBody(BaseModel):
    system: str
    week: str
    results: list[FromPairingsResultItem]


@router.post("/league/results/from-pairings")
def admin_league_results_from_pairings(
    body: FromPairingsResultsBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Create LeagueResult rows from a week's pairings in one shot. `result`'s
    "Player 1"/"Player 2" map to the pairing's a-side/b-side respectively."""
    _require_system_scope(body.system, user, db)

    system_id = _resolve_system_id(db, user.club_id, body.system)
    if system_id is None:
        raise HTTPException(status_code=422, detail="No league is enabled for this club.")
    season_id = _current_season_id(db, user.club_id, system_id)
    if season_id is None:
        raise HTTPException(status_code=422, detail="No league season is configured.")

    result_date = datetime.utcnow().strftime("%d/%m/%Y")
    created = 0
    skipped: list[dict] = []

    for item in body.results:
        if item.result not in VALID_RESULTS:
            raise HTTPException(status_code=422, detail=f"Invalid result: {item.result!r}")
        if item.game_type not in VALID_GAME_TYPES:
            raise HTTPException(status_code=422, detail="Game type must be Casual or Competitive.")
        p1_painting = _normalise_optional(item.player_1_painting_bonus)
        p2_painting = _normalise_optional(item.player_2_painting_bonus)
        if p1_painting not in VALID_PAINTING or p2_painting not in VALID_PAINTING:
            raise HTTPException(status_code=422, detail="Invalid painting bonus.")

        pairing = db.get(Pairing, item.pairing_id)
        if (pairing is None or pairing.club_id != user.club_id
                or pairing.system != body.system or pairing.week != body.week):
            skipped.append({"pairing_id": item.pairing_id, "reason": "not found"})
            continue
        if pairing.b_signup_id is None:
            skipped.append({"pairing_id": item.pairing_id, "reason": "bye"})
            continue

        a_su = db.get(Signup, pairing.a_signup_id)
        b_su = db.get(Signup, pairing.b_signup_id)
        if a_su is None or b_su is None or a_su.player_id is None or b_su.player_id is None:
            skipped.append({"pairing_id": item.pairing_id, "reason": "no linked player"})
            continue
        if a_su.player_id == b_su.player_id:
            skipped.append({"pairing_id": item.pairing_id, "reason": "same player"})
            continue

        # Idempotency: skip a game that is already logged, whether by an earlier
        # run of this or by one of the two players, in either order.
        already = find_recent_result(db, user.club_id, system_id, season_id, a_su.player_id, b_su.player_id)
        if already is not None:
            skipped.append({"pairing_id": item.pairing_id, "reason": "already logged"})
            continue

        db.add(LeagueResult(
            player_1_id=a_su.player_id,
            player_1_name=a_su.player_name,
            player_2_id=b_su.player_id,
            player_2_name=b_su.player_name,
            result=item.result,
            result_date=result_date,
            player_1_faction=_normalise_optional(pairing.a_faction or a_su.faction),
            player_2_faction=_normalise_optional(pairing.b_faction or b_su.faction),
            player_1_painting_bonus=p1_painting,
            player_2_painting_bonus=p2_painting,
            game_type=item.game_type,
            club_id=user.club_id,
            system_id=system_id,
            season_id=season_id,
        ))
        created += 1

    if created:
        db.flush()  # assign ids so the replay includes the new rows
        _recalculate_ratings(db, user.club_id, system_id, season_id)
        db.commit()

    return {"ok": True, "created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# League scoring config + seasons — per-(club, system), self-service by that
# system's admin (same _require_system_scope gate as the results endpoints).
# ---------------------------------------------------------------------------

def _league_config_row(cfg: LeagueConfig) -> dict:
    return {
        "scoring_method": cfg.scoring_method,
        "starting_rating": cfg.starting_rating,
        "k_casual": cfg.k_casual,
        "k_competitive": cfg.k_competitive,
        "single_k": cfg.single_k,
        "k_single": cfg.k_single,
        "painting_enabled": cfg.painting_enabled,
        "painting_fully_bonus": cfg.painting_fully_bonus,
        "painting_partial_bonus": cfg.painting_partial_bonus,
        "points_win": cfg.points_win,
        "points_draw": cfg.points_draw,
        "points_loss": cfg.points_loss,
    }


@router.get("/league-config")
def get_league_config(
    system: str,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """This club's league_enabled flag + scoring config for one system.
    Config returns the unsaved-defaults shape (matches the original
    hardcoded ELO) if the club hasn't customised it yet — same
    "defaults until saved" convention as SystemConfig."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    cfg = _get_league_config(db, user.club_id, config.id)
    return {"league_enabled": bool(cs and cs.league_enabled), **_league_config_row(cfg)}


class LeagueSettingsBody(BaseModel):
    system: str
    league_enabled: bool


@router.post("/league-settings")
def update_league_settings(
    body: LeagueSettingsBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Enable/disable this club's league for one system (mirrors
    POST /admin/missions-settings). Disabling does not delete any data —
    results/ratings/seasons/config are all preserved, just inactive."""
    _require_system_scope(body.system, user, db)
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")
    cs.league_enabled = body.league_enabled
    db.add(cs)
    db.commit()
    return {"ok": True}


VALID_SCORING_METHODS = {"elo", "winloss"}


class LeagueConfigBody(BaseModel):
    system: str
    scoring_method: str = "elo"
    starting_rating: float = 1000.0
    k_casual: int = 10
    k_competitive: int = 40
    single_k: bool = False
    k_single: int = 32
    painting_enabled: bool = True
    painting_fully_bonus: float = 3.0
    painting_partial_bonus: float = 1.0
    points_win: float = 3.0
    points_draw: float = 1.0
    points_loss: float = 0.0


@router.post("/league-config")
def save_league_config(
    body: LeagueConfigBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Upsert this club's scoring config for one system's league, then replay
    the current season's ratings under the new config (a config change must
    take effect immediately, not just for future results)."""
    _require_system_scope(body.system, user, db)
    if body.scoring_method not in VALID_SCORING_METHODS:
        raise HTTPException(
            status_code=422,
            detail=f"scoring_method must be one of {sorted(VALID_SCORING_METHODS)}.",
        )
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    cfg = db.exec(
        select(LeagueConfig).where(
            LeagueConfig.club_id == user.club_id, LeagueConfig.system_id == config.id
        )
    ).first()
    if cfg is None:
        cfg = LeagueConfig(club_id=user.club_id, system_id=config.id)
    for field in LeagueConfigBody.model_fields:
        if field == "system":
            continue
        setattr(cfg, field, getattr(body, field))
    db.add(cfg)
    db.commit()

    season_id = _current_season_id(db, user.club_id, config.id)
    if season_id is not None:
        _recalculate_ratings(db, user.club_id, config.id, season_id)
        db.commit()
    return _league_config_row(cfg)
