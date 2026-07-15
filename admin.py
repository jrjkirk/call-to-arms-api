"""Admin endpoints: role management and scoped access.

Permission model:
- Super-admin: user.is_super_admin == True. Set by SQL, never via this API.
  Can do everything and is the only role that can appoint/remove scope admins.
- Scope admin: a row in admin_roles (user_id, scope). One row per scope held.
"""
import os
import re
from datetime import date
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    admin_scopes,
    club_runnable_scopes,
    current_user,
    get_session,
    require_platform_admin,
    require_scope,
    require_super_admin,
    require_user,
    valid_scopes,
)
from database import scoped
from league import (
    VALID_GAME_TYPES,
    VALID_PAINTING,
    VALID_RESULTS,
    _normalise_optional,
    _recalculate_ratings,
)
from models import AdminRole, Club, ClubSetting, ClubSystem, ClubWebhook, LeagueResult, PairingBlock, Pairing, Player, PublishState, Signup, SystemConfig, User
from services import LEAGUE_ANNOUNCED_ACHIEVEMENTS, player_titles, post_discord_achievement, set_player_titles
from pairings_engine import generate
from signups import (
    EXPERIENCE_OPTIONS,
    HH_VIBES,
    SCENARIO_OPTIONS,
    SYSTEMS,
    TOW_VIBES,
    _require_system_enabled,
    _validate_week,
)
from week_logic import _DAY_NAME_TO_INT

GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "")
GH_REPO = os.environ.get("GH_REPO", "jrjkirk/call-to-arms-api")
GH_PAIRINGS_SCREENSHOT_WORKFLOW = "post-pairings-image.yml"

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_any_admin(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
) -> User:
    """403 unless the caller has at least one admin scope (includes super-admins)."""
    if not admin_scopes(user, db):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


@router.get("/me")
def admin_me(
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Return the caller's admin status. Always 200; unauthenticated = no access."""
    scopes = admin_scopes(user, db)
    return {
        "is_super_admin": user.is_super_admin if user else False,
        "is_platform_admin": user.is_platform_admin if user else False,
        "scopes": sorted(scopes),
    }


@router.get("/roles")
def list_roles(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """All current admin_roles rows joined to user info, plus super-admin list."""
    role_rows = db.exec(
        select(AdminRole, User)
        .join(User, User.id == AdminRole.user_id)
        .where(AdminRole.club_id == user.club_id)
        .order_by(User.discord_name)
    ).all()

    roles = []
    for role, role_user in role_rows:
        player_name = None
        if role_user.player_id:
            p = db.get(Player, role_user.player_id)
            player_name = p.name if p else None
        roles.append({
            "user_id": role.user_id,
            "discord_name": role_user.discord_name,
            "player_name": player_name,
            "scope": role.scope,
        })

    super_admins_rows = db.exec(
        scoped(User, user.club_id).where(User.is_super_admin == True).order_by(User.discord_name)
    ).all()
    super_admins = []
    for sa in super_admins_rows:
        player_name = None
        if sa.player_id:
            p = db.get(Player, sa.player_id)
            player_name = p.name if p else None
        super_admins.append({
            "user_id": sa.id,
            "discord_name": sa.discord_name,
            "player_name": player_name,
        })

    return {"roles": roles, "super_admins": super_admins}


@router.get("/grantable-users")
def grantable_users(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Active users with a linked player — the candidate list for the appoint UI."""
    users = db.exec(
        scoped(User, user.club_id).where(User.player_id.isnot(None))
    ).all()

    result = []
    for u in users:
        p = db.get(Player, u.player_id)
        if p and p.active:
            result.append({
                "id": u.id,
                "discord_name": u.discord_name,
                "player_name": p.name,
            })

    result.sort(key=lambda x: x["player_name"].lower())
    return result


class RoleBody(BaseModel):
    user_id: int
    scope: str


@router.post("/roles")
def grant_role(
    body: RoleBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Idempotent: insert (user_id, scope) if not already present."""
    scopes = valid_scopes(db)
    if body.scope not in scopes:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scope. Must be one of: {sorted(scopes)}",
        )

    runnable = club_runnable_scopes(user.club_id, db)
    if body.scope not in runnable:
        raise HTTPException(
            status_code=422,
            detail=f"Your club does not run {body.scope!r}. Must be one of: {sorted(runnable)}",
        )

    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    existing = db.exec(
        scoped(AdminRole, user.club_id)
        .where(AdminRole.user_id == body.user_id)
        .where(AdminRole.scope == body.scope)
    ).first()

    if existing is None:
        db.add(AdminRole(user_id=body.user_id, scope=body.scope, club_id=user.club_id))
        db.commit()

    return {"ok": True}


@router.delete("/roles")
def remove_role(
    user_id: int,
    scope: str,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Delete the (user_id, scope) row if present."""
    row = db.exec(
        scoped(AdminRole, user.club_id)
        .where(AdminRole.user_id == user_id)
        .where(AdminRole.scope == scope)
    ).first()

    if row is not None:
        db.delete(row)
        db.commit()
        return {"ok": True, "removed": True}

    return {"ok": True, "removed": False}


@router.get("/players")
def admin_players(
    scope: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Scoped read (scope provided) or global super-admin read (scope omitted).

    scope provided: 403 unless the caller holds that scope; returns active players.
    scope omitted:  403 unless super-admin; returns all players with full profile
                    fields so the edit-player panel can pre-fill forms.
    """
    if scope is not None:
        if scope not in valid_scopes(db):
            raise HTTPException(status_code=422, detail="Invalid scope.")
        if scope not in admin_scopes(user, db):
            raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")
        players = db.exec(
            scoped(Player, user.club_id).where(Player.active == True).order_by(Player.name)
        ).all()
        return [{"id": p.id, "name": p.name, "active": p.active} for p in players]

    # No scope — super-admin only, full player list for the edit-player panel.
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="Super-admin access required.")
    players = db.exec(scoped(Player, user.club_id).order_by(Player.name)).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "titles": player_titles(p),
            "active": p.active,
            "admin_notes": p.admin_notes,
        }
        for p in players
    ]


class PatchPlayerBody(BaseModel):
    name: Optional[str] = None
    titles: Optional[list[str]] = None
    active: Optional[bool] = None
    admin_notes: Optional[str] = None


@router.patch("/players/{player_id}")
def patch_player(
    player_id: int,
    body: PatchPlayerBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    player = db.get(Player, player_id)
    if not player or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found.")

    if body.name is not None:
        stripped = body.name.strip()
        if stripped:
            player.name = stripped

    if body.titles is not None:
        set_player_titles(player, body.titles)

    if body.active is not None:
        player.active = body.active

    if body.admin_notes is not None:
        player.admin_notes = body.admin_notes.strip() or None

    db.add(player)
    db.commit()
    db.refresh(player)

    return {
        "id": player.id,
        "name": player.name,
        "titles": player_titles(player),
        "active": player.active,
        "admin_notes": player.admin_notes,
    }


# ---------------------------------------------------------------------------
# Pairing blocks (global — no system column, canonical low < high storage)
# ---------------------------------------------------------------------------

@router.get("/blocks/players")
def block_players(
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """All active players — used to populate the add-block form dropdowns."""
    players = db.exec(
        scoped(Player, user.club_id).where(Player.active == True).order_by(Player.name)
    ).all()
    return [{"id": p.id, "name": p.name} for p in players]


@router.get("/blocks")
def list_blocks(
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """All pairing blocks, enriched with player names, sorted A→B."""
    block_rows = db.exec(scoped(PairingBlock, user.club_id)).all()

    player_ids = {b.player_a_id for b in block_rows} | {b.player_b_id for b in block_rows}
    players_by_id: dict[int, Player] = {}
    if player_ids:
        rows = db.exec(select(Player).where(Player.id.in_(player_ids))).all()
        players_by_id = {p.id: p for p in rows}

    def _name(pid: int) -> str:
        p = players_by_id.get(pid)
        return p.name if p else f"#{pid}"

    result = [
        {
            "block_id": b.id,
            "player_a_id": b.player_a_id,
            "player_a_name": _name(b.player_a_id),
            "player_b_id": b.player_b_id,
            "player_b_name": _name(b.player_b_id),
            "note": b.note,
        }
        for b in block_rows
    ]
    result.sort(key=lambda x: (x["player_a_name"].lower(), x["player_b_name"].lower()))
    return result


class BlockBody(BaseModel):
    player_a_id: int
    player_b_id: int
    note: Optional[str] = None


@router.post("/blocks")
def add_block(
    body: BlockBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Insert a canonical (low, high) block. Idempotent; updates note if block exists."""
    if body.player_a_id == body.player_b_id:
        raise HTTPException(status_code=422, detail="Cannot block a player from themselves.")

    low, high = sorted([body.player_a_id, body.player_b_id])
    note = (body.note or "").strip() or None

    existing = db.exec(
        scoped(PairingBlock, user.club_id)
        .where(PairingBlock.player_a_id == low)
        .where(PairingBlock.player_b_id == high)
    ).first()

    if existing is not None:
        if note:
            existing.note = note
            db.add(existing)
            db.commit()
        return {"ok": True, "created": False}

    db.add(PairingBlock(player_a_id=low, player_b_id=high, note=note, club_id=user.club_id))
    db.commit()
    return {"ok": True, "created": True}


@router.delete("/blocks")
def remove_block(
    player_a_id: int,
    player_b_id: int,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Delete the canonical (low, high) block if it exists."""
    low, high = sorted([player_a_id, player_b_id])
    row = db.exec(
        scoped(PairingBlock, user.club_id)
        .where(PairingBlock.player_a_id == low)
        .where(PairingBlock.player_b_id == high)
    ).first()

    if row is not None:
        db.delete(row)
        db.commit()
        return {"ok": True, "removed": True}

    return {"ok": True, "removed": False}


# ---------------------------------------------------------------------------
# Game history (read-only, per-scope)
# ---------------------------------------------------------------------------

@router.get("/history")
def admin_history(
    scope: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Recent game history for a scope. 403 unless caller holds that scope."""
    if scope not in valid_scopes(db):
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if scope not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")

    if scope == "League":
        rows = db.exec(
            scoped(LeagueResult, user.club_id).order_by(LeagueResult.id.desc()).limit(100)
        ).all()
        return [
            {
                "date": r.result_date,
                "p1_name": r.player_1_name,
                "p1_faction": r.player_1_faction,
                "p2_name": r.player_2_name,
                "p2_faction": r.player_2_faction,
                "result": r.result,
                "game_type": r.game_type,
            }
            for r in rows
        ]

    # System scope: join pairings to signups for player names/factions
    pairings = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.system == scope)
        .order_by(Pairing.id.desc())
        .limit(100)
    ).all()

    signup_ids = {p.a_signup_id for p in pairings} | {
        p.b_signup_id for p in pairings if p.b_signup_id
    }
    signups_by_id: dict[int, Signup] = {}
    if signup_ids:
        signup_rows = db.exec(select(Signup).where(Signup.id.in_(signup_ids))).all()
        signups_by_id = {s.id: s for s in signup_rows}

    result = []
    for p in pairings:
        a = signups_by_id.get(p.a_signup_id)
        b = signups_by_id.get(p.b_signup_id) if p.b_signup_id else None
        result.append({
            "week": p.week,
            "player_a_name": a.player_name if a else f"#{p.a_signup_id}",
            "player_a_faction": p.a_faction or (a.faction if a else None),
            "player_b_name": b.player_name if b else None,
            "player_b_faction": (p.b_faction or (b.faction if b else None)) if b else None,
        })
    return result


# ---------------------------------------------------------------------------
# Pairings — admin generation, editing, publishing
# ---------------------------------------------------------------------------

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


def _eta_show(a_su: Optional[Signup], b_su: Optional[Signup]) -> Optional[str]:
    a_eta = a_su.eta if a_su else None
    b_eta = b_su.eta if b_su else None
    if a_eta and b_eta:
        return max(a_eta, b_eta)
    return a_eta or b_eta


def _pts_show(a_su: Optional[Signup], b_su: Optional[Signup], system: str) -> Optional[str]:
    if system == "Kill Team":
        return None
    vals = [su.points for su in (a_su, b_su) if su is not None and isinstance(su.points, int)]
    return str(min(vals)) if vals else None


def _build_display_row(
    row_id: Optional[int],
    a_signup_id: int,
    b_signup_id: Optional[int],
    a_faction: Optional[str],
    b_faction: Optional[str],
    prearranged: bool,
    signups_by_id: dict,
    system: str,
) -> dict:
    a_su = signups_by_id.get(a_signup_id)
    b_su = signups_by_id.get(b_signup_id) if b_signup_id else None

    a_name = a_su.player_name if a_su else f"#{a_signup_id}"
    a_vibe = a_su.vibe if a_su else None

    b_name = (b_su.player_name if b_su else f"#{b_signup_id}") if b_signup_id else "BYE"
    b_vibe = b_su.vibe if b_su else None

    return {
        "id": row_id,
        "a_signup_id": a_signup_id,
        "a_name": a_name,
        "a_faction": a_faction if a_faction is not None else (a_su.faction if a_su else None),
        "a_vibe": a_vibe,
        "b_signup_id": b_signup_id,
        "b_name": b_name,
        "b_faction": b_faction if (b_signup_id and b_faction is not None) else (b_su.faction if b_su else None),
        "b_vibe": b_vibe,
        "type": _public_vibe_display(a_vibe, b_vibe),
        "eta": _eta_show(a_su, b_su),
        "points": _pts_show(a_su, b_su, system),
        "prearranged": prearranged,
    }


def _collect_signups_for_rows(rows, db: Session) -> dict:
    ids: set[int] = set()
    for r in rows:
        if isinstance(r, Pairing):
            ids.add(r.a_signup_id)
            if r.b_signup_id:
                ids.add(r.b_signup_id)
        else:
            if r.get("a_signup_id"):
                ids.add(r["a_signup_id"])
            if r.get("b_signup_id"):
                ids.add(r["b_signup_id"])
    if not ids:
        return {}
    rows_q = db.exec(select(Signup).where(Signup.id.in_(ids))).all()
    return {s.id: s for s in rows_q}


def _pairing_rows_to_display(pairings: list, signups_by_id: dict, system: str) -> list:
    result = []
    for p in pairings:
        a_faction = p.a_faction or (signups_by_id.get(p.a_signup_id, None) and signups_by_id[p.a_signup_id].faction)
        b_faction = None
        if p.b_signup_id:
            b_faction = p.b_faction or (signups_by_id.get(p.b_signup_id, None) and signups_by_id[p.b_signup_id].faction)
        result.append(_build_display_row(
            p.id, p.a_signup_id, p.b_signup_id,
            a_faction, b_faction,
            p.prearranged, signups_by_id, system,
        ))
    return result


def _dicts_to_display(dicts: list, signups_by_id: dict, system: str) -> list:
    return [
        _build_display_row(
            None,
            d["a_signup_id"], d.get("b_signup_id"),
            d.get("a_faction"), d.get("b_faction"),
            False, signups_by_id, system,
        )
        for d in dicts
    ]

_VALID_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_TIME_RE = re.compile(r"^(2[0-3]|[01]\d):[0-5]\d$")


def _slug(system: str) -> str:
    return system.replace(" ", "").replace("'", "")


def _get_setting(db: Session, club_id: int, key: str, default: Optional[str] = None) -> Optional[str]:
    row = db.get(ClubSetting, (club_id, key))
    return row.value if row is not None else default


def _upsert_setting(db: Session, club_id: int, key: str, value: str) -> None:
    row = db.get(ClubSetting, (club_id, key))
    if row is None:
        row = ClubSetting(club_id=club_id, key=key, value=value)
    else:
        row.value = value
    db.add(row)


def _require_system_scope(system: str, user: User, db: Session) -> None:
    if system not in valid_scopes(db):
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if system not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{system}' required.")


class AutoPairingsSettingsBody(BaseModel):
    system: str
    enabled: bool
    day: str
    time: str


@router.get("/auto-pairings-settings")
def get_auto_pairings_settings(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    _require_system_scope(system, user, db)
    slug = _slug(system)
    enabled_str = (_get_setting(db, user.club_id, f"auto_pairings_{slug}_enabled", "false") or "false").lower()
    return {
        "enabled": enabled_str == "true",
        "day": _get_setting(db, user.club_id, f"auto_pairings_{slug}_day", "Tuesday") or "Tuesday",
        "time": _get_setting(db, user.club_id, f"auto_pairings_{slug}_time", "20:00") or "20:00",
        "last_week": _get_setting(db, user.club_id, f"auto_pairings_{slug}_last_week"),
    }


@router.post("/auto-pairings-settings")
def post_auto_pairings_settings(
    body: AutoPairingsSettingsBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    _require_system_scope(body.system, user, db)
    if body.day not in _VALID_DAYS:
        raise HTTPException(status_code=422, detail=f"day must be one of {_VALID_DAYS}")
    if not _TIME_RE.match(body.time):
        raise HTTPException(status_code=422, detail="time must match HH:MM (00-23 / 00-59)")
    slug = _slug(body.system)
    _upsert_setting(db, user.club_id, f"auto_pairings_{slug}_enabled", "true" if body.enabled else "false")
    _upsert_setting(db, user.club_id, f"auto_pairings_{slug}_day", body.day)
    _upsert_setting(db, user.club_id, f"auto_pairings_{slug}_time", body.time)
    db.commit()
    return {"ok": True}


class PairingsWeekBody(BaseModel):
    system: str
    week: str


class PublishBody(BaseModel):
    system: str
    week: str
    published: bool


class DeletePairingsBody(BaseModel):
    system: str
    week: str
    ids: list[int]


class PairingSaveRow(BaseModel):
    id: int
    a_signup_id: Optional[int] = None
    b_signup_id: Optional[int] = None
    a_faction: Optional[str] = None
    b_faction: Optional[str] = None
    a_type: Optional[str] = None
    b_type: Optional[str] = None
    type: Optional[str] = None
    eta: Optional[str] = None
    points: Optional[Any] = None


class PairingSaveBody(BaseModel):
    system: str
    week: str
    rows: list[PairingSaveRow]


@router.get("/pairings/signup-list")
def pairings_signup_list(
    system: str,
    week: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """De-duped signup list for a week/system — used by the admin grid dropdowns."""
    _require_system_scope(system, user, db)

    all_signups = db.exec(
        scoped(Signup, user.club_id)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at)
    ).all()

    seen: dict[str, Signup] = {}
    from pairings_engine import _normalize_name
    for su in all_signups:
        seen[_normalize_name(su.player_name).lower()] = su

    return [
        {"id": su.id, "name": su.player_name, "faction": su.faction, "vibe": su.vibe}
        for su in seen.values()
    ]


@router.post("/pairings/preview")
def pairings_preview(
    body: PairingsWeekBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """DRY RUN — compute proposed pairings without writing to the DB.

    Returns existing prearranged rows (with real IDs) + proposed rows (id=null).
    """
    _require_system_scope(body.system, user, db)

    prearranged = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.week == body.week)
        .where(Pairing.system == body.system)
        .where(Pairing.prearranged == True)
        .order_by(Pairing.id)
    ).all()

    proposed_dicts = generate(db, body.week, body.system, persist=False, club_id=user.club_id)

    all_rows = list(prearranged) + proposed_dicts
    signups_by_id = _collect_signups_for_rows(all_rows, db)

    display = _pairing_rows_to_display(prearranged, signups_by_id, body.system) + \
              _dicts_to_display(proposed_dicts, signups_by_id, body.system)

    return {"rows": display, "preview": True}


@router.post("/pairings/generate")
def pairings_generate(
    body: PairingsWeekBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Delete existing pending (non-prearranged) pairings, then generate and persist new ones."""
    _require_system_scope(body.system, user, db)
    _require_system_enabled(db, user.club_id, body.system)

    old = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.week == body.week)
        .where(Pairing.system == body.system)
        .where(Pairing.status == "pending")
        .where(Pairing.prearranged != True)
    ).all()
    for p in old:
        db.delete(p)
    # autoflush will sync deletes before generate queries

    new_pairings = generate(db, body.week, body.system, persist=True, club_id=user.club_id)

    signups_by_id = _collect_signups_for_rows(new_pairings, db)
    display = _pairing_rows_to_display(new_pairings, signups_by_id, body.system)
    return {"rows": display}


@router.get("/pairings")
def pairings_get(
    system: str,
    week: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Return all saved pairings for a week/system, plus publish state."""
    _require_system_scope(system, user, db)

    rows = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .order_by(Pairing.id)
    ).all()

    gate = db.exec(
        scoped(PublishState, user.club_id)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()
    published = gate.published if gate else False

    signups_by_id = _collect_signups_for_rows(rows, db)
    display = _pairing_rows_to_display(rows, signups_by_id, system)
    return {"rows": display, "published": published}


@router.post("/pairings/publish")
def pairings_publish(
    body: PublishBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Upsert PublishState.published for a week/system."""
    _require_system_scope(body.system, user, db)

    gate = db.exec(
        scoped(PublishState, user.club_id)
        .where(PublishState.week == body.week)
        .where(PublishState.system == body.system)
    ).first()

    if gate is None:
        gate = PublishState(
            week=body.week,
            system=body.system,
            published=body.published,
            club_id=user.club_id,
        )
    else:
        gate.published = body.published
    db.add(gate)
    db.commit()
    return {"ok": True, "published": body.published}


@router.post("/pairings/save")
def pairings_save(
    body: PairingSaveBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Write grid edits back to Pairing rows and their underlying Signup rows."""
    _require_system_scope(body.system, user, db)

    changed = 0
    for row in body.rows:
        p = db.get(Pairing, row.id)
        if p is None:
            continue
        if p.week != body.week or p.system != body.system or p.club_id != user.club_id:
            continue

        if row.a_signup_id is not None:
            p.a_signup_id = row.a_signup_id
        if row.b_signup_id is not None:
            p.b_signup_id = row.b_signup_id

        a_su = db.get(Signup, p.a_signup_id) if p.a_signup_id else None
        b_su = db.get(Signup, p.b_signup_id) if p.b_signup_id else None

        # Faction — "— None —" sentinel → None
        def _clean_faction(f: Optional[str]) -> Optional[str]:
            if f in (None, "— None —", ""):
                return None
            return f

        a_faction = _clean_faction(row.a_faction)
        b_faction = _clean_faction(row.b_faction)

        p.a_faction = a_faction if p.a_signup_id else None
        p.b_faction = b_faction if p.b_signup_id else None

        if a_su:
            a_su.faction = a_faction
            db.add(a_su)
        if b_su:
            b_su.faction = b_faction
            db.add(b_su)

        # Vibe — per-side type if non-empty, else shared type
        a_type_raw = (row.a_type or "").strip()
        b_type_raw = (row.b_type or "").strip()
        shared_type = (row.type or "").strip()
        a_vibe = a_type_raw if a_type_raw else (shared_type or None)
        b_vibe = b_type_raw if b_type_raw else (shared_type or None)
        if a_su and a_vibe:
            a_su.vibe = a_vibe
            db.add(a_su)
        if b_su and b_vibe:
            b_su.vibe = b_vibe
            db.add(b_su)

        # ETA and points — written to both present signups
        eta_val = (row.eta or "").strip() or None
        pts_val: Optional[int] = None
        try:
            if row.points is not None and str(row.points).strip():
                pts_val = int(row.points)
        except (ValueError, TypeError):
            pass

        for su in [a_su, b_su]:
            if su is None:
                continue
            if eta_val:
                su.eta = eta_val
                db.add(su)
            if pts_val is not None:
                su.points = pts_val
                db.add(su)

        db.add(p)
        changed += 1

    db.commit()
    return {"changed": changed}


@router.delete("/pairings")
def pairings_delete(
    body: DeletePairingsBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Delete specific pairings by ID, scoped to this week/system."""
    _require_system_scope(body.system, user, db)

    deleted = 0
    for pid in body.ids:
        p = db.get(Pairing, pid)
        if p is None:
            continue
        if p.week != body.week or p.system != body.system or p.club_id != user.club_id:
            continue
        db.delete(p)
        deleted += 1

    if deleted:
        db.commit()
    return {"deleted": deleted}


@router.post("/pairings/post-discord")
def pairings_post_discord(
    body: PairingsWeekBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Trigger the GitHub Actions workflow that screenshots the public
    /pairings page for this system/week and posts it to that system's
    Discord channel.

    Fire-and-forget: a successful response means the workflow was queued,
    not that the Discord post has happened yet — Playwright + Chromium
    install takes roughly 30-60 seconds to run in CI.
    """
    _require_system_scope(body.system, user, db)

    if not GH_DISPATCH_TOKEN:
        return {"posted": False, "reason": "no dispatch token configured"}

    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_PAIRINGS_SCREENSHOT_WORKFLOW}/dispatches"
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"ref": "main", "inputs": {"system": body.system, "week": body.week}},
            timeout=10.0,
        )
    except Exception:
        return {"posted": False, "reason": "GitHub API request failed"}

    if resp.status_code != 204:
        return {"posted": False, "reason": f"GitHub API returned {resp.status_code}"}

    return {"posted": True, "queued": True}


# ---------------------------------------------------------------------------
# Signup management — admin read/write for a week's signups
# ---------------------------------------------------------------------------

def _signup_row(su: Signup) -> dict:
    return {
        "id": su.id,
        "player_id": su.player_id,
        "player_name": su.player_name,
        "faction": su.faction,
        "points": su.points,
        "eta": su.eta,
        "experience": su.experience,
        "vibe": su.vibe,
        "standby_ok": su.standby_ok,
        "scenario": su.scenario,
        "can_demo": su.can_demo,
    }


def _system_config(system: str) -> dict:
    """Per-system field visibility config for the admin signup editor."""
    if system == "Kill Team":
        return {
            "show_points": False,
            "default_points": None,
            "show_scenario": False,
            "show_standby": False,
            "show_can_demo": False,
            "vibe_options": ["Standard"],
            "vibe_fixed": "Standard",
        }
    if system == "The Horus Heresy":
        return {
            "show_points": True,
            "default_points": 3000,
            "show_scenario": False,
            "show_standby": False,
            "show_can_demo": True,
            "vibe_options": sorted(HH_VIBES),
            "vibe_fixed": None,
        }
    # The Old World (and any future system)
    return {
        "show_points": True,
        "default_points": 2000,
        "show_scenario": True,
        "show_standby": True,
        "show_can_demo": True,
        "vibe_options": sorted(TOW_VIBES),
        "vibe_fixed": None,
    }


@router.get("/signups")
def admin_signups_list(
    system: str,
    week: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """All signups for a week/system, ordered by player_name, with per-system field config."""
    _require_system_scope(system, user, db)

    rows = db.exec(
        scoped(Signup, user.club_id)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.player_name)
    ).all()

    return {
        "signups": [_signup_row(su) for su in rows],
        "config": _system_config(system),
    }


class AdminSignupPatch(BaseModel):
    faction: Optional[str] = None
    points: Optional[int] = None
    eta: Optional[str] = None
    experience: Optional[str] = None
    vibe: Optional[str] = None
    standby_ok: Optional[bool] = None
    scenario: Optional[str] = None
    can_demo: Optional[bool] = None


@router.patch("/signups/{signup_id}")
def admin_signup_patch(
    signup_id: int,
    body: AdminSignupPatch,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Partial update of a signup row.

    player_id and player_name are NOT editable — silently ignored if present in
    the JSON body (Pydantic strips them because they are not in AdminSignupPatch).
    All other fields use the same per-system normalisation as POST /signups:
    invalid vibe → system default; points clamped 0-10000; invalid experience → 'New';
    '— None —' / '' normalises faction/scenario to null.
    Only fields present in the request body are updated; omitted fields are left unchanged.
    """
    su = db.get(Signup, signup_id)
    if su is None or su.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Signup not found.")

    _require_system_scope(su.system, user, db)

    provided = body.model_fields_set
    is_kt = su.system == "Kill Team"
    is_hh = su.system == "The Horus Heresy"

    if "faction" in provided:
        f = body.faction
        su.faction = None if f in (None, "", "— None —") else f

    if "points" in provided:
        if is_kt or body.points is None:
            su.points = None if is_kt else None
        else:
            su.points = max(0, min(int(body.points), 10000))

    if "eta" in provided:
        su.eta = (body.eta or "").strip() or None

    if "experience" in provided:
        su.experience = body.experience if body.experience in EXPERIENCE_OPTIONS else "New"

    if "vibe" in provided:
        if is_kt:
            su.vibe = "Standard"
        elif is_hh:
            su.vibe = body.vibe if body.vibe in HH_VIBES else "Standard"
        else:
            su.vibe = body.vibe if body.vibe in TOW_VIBES else "Casual"

    if "standby_ok" in provided:
        su.standby_ok = bool(body.standby_ok) if body.standby_ok is not None else False

    if "scenario" in provided:
        if is_kt or is_hh:
            su.scenario = None
        else:
            s = body.scenario
            su.scenario = None if s in (None, "", "— None —") else s

    if "can_demo" in provided:
        su.can_demo = False if is_kt else (bool(body.can_demo) if body.can_demo is not None else False)

    db.add(su)
    db.commit()
    db.refresh(su)
    return _signup_row(su)


class AdminSignupCreate(BaseModel):
    system: str
    week: str
    player_id: int
    faction: Optional[str] = None
    points: Optional[int] = None
    eta: Optional[str] = None
    experience: Optional[str] = None
    vibe: Optional[str] = None
    standby_ok: bool = False
    scenario: Optional[str] = None
    can_demo: bool = False


@router.post("/signups", status_code=201)
def admin_signup_create(
    body: AdminSignupCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Add a signup on behalf of a player (admin correction / manual entry).

    Unlike POST /signups, this does NOT upsert — a 409 is returned if the player
    already has a signup for this week/system so the admin uses PATCH instead.
    Discord signup webhook is NOT fired (avoid spurious 'X signed up' posts for
    admin corrections).
    Per-system defaults and normalisation match the regular POST /signups exactly.
    """
    if body.system not in SYSTEMS:
        raise HTTPException(status_code=422, detail="Unknown system.")

    _require_system_scope(body.system, user, db)
    _require_system_enabled(db, user.club_id, body.system)
    week = _validate_week(body.week)

    player = db.get(Player, body.player_id)
    if player is None or not player.active or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found.")

    existing = db.exec(
        scoped(Signup, user.club_id)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == body.player_id)
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This player is already signed up for this week — edit their existing row instead.",
        )

    is_kt = body.system == "Kill Team"
    is_hh = body.system == "The Horus Heresy"

    faction = body.faction
    if faction in (None, "", "— None —"):
        faction = None

    experience = body.experience if body.experience in EXPERIENCE_OPTIONS else "New"
    eta = (body.eta or "").strip() or None

    if is_kt:
        vibe = "Standard"
        points = None
        scenario = None
        can_demo = False
        standby_ok = False
    elif is_hh:
        vibe = body.vibe if body.vibe in HH_VIBES else "Standard"
        points = max(0, min(int(body.points or 3000), 10000))
        scenario = None
        can_demo = bool(body.can_demo)
        standby_ok = False
    else:
        vibe = body.vibe if body.vibe in TOW_VIBES else "Casual"
        points = max(0, min(int(body.points or 2000), 10000))
        s = body.scenario
        scenario = None if s in (None, "", "— None —") else s
        can_demo = bool(body.can_demo)
        standby_ok = bool(body.standby_ok)

    su = Signup(
        week=week,
        system=body.system,
        player_id=player.id,
        player_name=player.name,
        faction=faction,
        points=points,
        eta=eta,
        experience=experience,
        vibe=vibe,
        standby_ok=standby_ok,
        tnt_ok=False,
        scenario=scenario,
        can_demo=can_demo,
        club_id=user.club_id,
    )
    db.add(su)
    db.commit()
    db.refresh(su)
    return _signup_row(su)


@router.delete("/signups/{signup_id}")
def admin_signup_delete(
    signup_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Force-drop a signup, bypassing the PublishState check that blocks player self-drops.

    Mirrors the regular drop logic: any prearranged=True Pairing referencing this
    signup is deleted (the other player's signup is untouched and re-enters the pool).
    Discord drop webhook is NOT fired (avoid spurious 'X dropped' posts for admin
    corrections).
    """
    su = db.get(Signup, signup_id)
    if su is None or su.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Signup not found.")

    _require_system_scope(su.system, user, db)

    prearranged = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.week == su.week)
        .where(Pairing.system == su.system)
        .where(Pairing.prearranged == True)
        .where((Pairing.a_signup_id == signup_id) | (Pairing.b_signup_id == signup_id))
    ).all()
    for p in prearranged:
        db.delete(p)

    db.delete(su)
    db.commit()
    return {"ok": True}


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
    user: User = Depends(require_scope("League")),
    db: Session = Depends(get_session),
):
    """All LeagueResult rows, newest first (display order; recalc internally is id-ascending)."""
    rows = db.exec(scoped(LeagueResult, user.club_id).order_by(LeagueResult.id.desc())).all()
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
    user: User = Depends(require_scope("League")),
    db: Session = Depends(get_session),
):
    """Partial update of a league result, followed by a full ratings replay."""
    row = db.get(LeagueResult, result_id)
    if row is None or row.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="League result not found.")

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
    _recalculate_ratings(db, user.club_id)
    db.commit()
    db.refresh(row)
    return _league_result_row(row)


@router.delete("/league/results/{result_id}")
def admin_league_result_delete(
    result_id: int,
    user: User = Depends(require_scope("League")),
    db: Session = Depends(get_session),
):
    """Delete a league result and replay ratings from scratch."""
    row = db.get(LeagueResult, result_id)
    if row is None or row.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="League result not found.")

    db.delete(row)
    db.flush()
    _recalculate_ratings(db, user.club_id)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Achievement announcements — admin manual post + options list
# ---------------------------------------------------------------------------

class AchievementPostBody(BaseModel):
    player_name: str
    achievement: str


@router.get("/achievements/options")
def achievement_options(
    _: User = Depends(require_super_admin),
):
    """Return the sorted list of achievements eligible for Discord announcement."""
    return {"achievements": sorted(LEAGUE_ANNOUNCED_ACHIEVEMENTS)}


@router.post("/achievements/post-discord")
def achievement_post_discord(
    body: AchievementPostBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Manually post an achievement unlock to Discord.

    No DB writes or announced-set checks — this is a direct override for
    admin corrections. Requires super-admin.
    """
    if body.achievement not in LEAGUE_ANNOUNCED_ACHIEVEMENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Achievement must be one of: {sorted(LEAGUE_ANNOUNCED_ACHIEVEMENTS)}",
        )
    post_discord_achievement(body.player_name, body.achievement, user.club_id, db)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Club webhooks — self-service (club-wide, not per-system scope)
# ---------------------------------------------------------------------------

WEBHOOK_TYPES_PER_SYSTEM: tuple[str, ...] = ("signup", "pairings", "call_to_arms")
WEBHOOK_TYPES_CLUB_LEVEL: tuple[str, ...] = ("league_result", "achievement", "league_rankings")
ALL_WEBHOOK_TYPES: frozenset[str] = frozenset(WEBHOOK_TYPES_PER_SYSTEM + WEBHOOK_TYPES_CLUB_LEVEL)


def _mask_webhook_row(row: Optional[ClubWebhook]) -> dict:
    """Never return the full URL — last 4 characters only, so an operator can
    sanity-check "is this the webhook I think it is" without re-exposing the
    secret. Encryption-at-rest is a separate, deferred hardening step; this
    masking is the actual security control for this slice."""
    if row is None or not row.url:
        return {"configured": False}
    return {"configured": True, "last_four": "..." + row.url[-4:]}


@router.get("/webhooks")
def list_club_webhooks(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Full grid of the six webhook types for the caller's own club — every
    (type, system) combination appears, whether or not a row exists yet, so
    the UI can show what's configurable, not just what's set."""
    systems = db.exec(select(SystemConfig).where(SystemConfig.active == True)).all()
    existing = db.exec(
        select(ClubWebhook).where(ClubWebhook.club_id == user.club_id)
    ).all()
    existing_by_key = {(r.webhook_type, r.system_id): r for r in existing}

    grid = []
    for webhook_type in WEBHOOK_TYPES_PER_SYSTEM:
        for system in systems:
            row = existing_by_key.get((webhook_type, system.id))
            grid.append({
                "webhook_type": webhook_type,
                "system_id": system.id,
                "system_name": system.name,
                **_mask_webhook_row(row),
            })
    for webhook_type in WEBHOOK_TYPES_CLUB_LEVEL:
        row = existing_by_key.get((webhook_type, None))
        grid.append({
            "webhook_type": webhook_type,
            "system_id": None,
            "system_name": None,
            **_mask_webhook_row(row),
        })
    return grid


class ClubWebhookBody(BaseModel):
    webhook_type: str
    system_id: Optional[int] = None
    url: str


@router.post("/webhooks")
def upsert_club_webhook(
    body: ClubWebhookBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Create/update the caller's own club's webhook for (webhook_type,
    system_id). club_id always comes from user.club_id, never the body —
    same non-negotiable rule as scoped() everywhere else in this codebase.
    Never returns the raw url, even in this same response."""
    if body.webhook_type not in ALL_WEBHOOK_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"webhook_type must be one of: {sorted(ALL_WEBHOOK_TYPES)}",
        )

    if body.webhook_type in WEBHOOK_TYPES_PER_SYSTEM:
        if body.system_id is None:
            raise HTTPException(
                status_code=422,
                detail=f"system_id is required for webhook_type={body.webhook_type!r}.",
            )
        system = db.get(SystemConfig, body.system_id)
        if system is None:
            raise HTTPException(status_code=404, detail="System not found.")
    else:
        if body.system_id is not None:
            raise HTTPException(
                status_code=422,
                detail=f"system_id must not be set for webhook_type={body.webhook_type!r}.",
            )

    if not body.url or not body.url.strip():
        raise HTTPException(status_code=422, detail="url must not be empty.")

    existing = db.exec(
        select(ClubWebhook).where(
            ClubWebhook.club_id == user.club_id,
            ClubWebhook.webhook_type == body.webhook_type,
            ClubWebhook.system_id == body.system_id,
        )
    ).first()

    if existing:
        existing.url = body.url
        db.add(existing)
        row = existing
    else:
        row = ClubWebhook(
            club_id=user.club_id,
            webhook_type=body.webhook_type,
            system_id=body.system_id,
            url=body.url,
        )
        db.add(row)

    db.commit()
    db.refresh(row)
    return {
        "webhook_type": row.webhook_type,
        "system_id": row.system_id,
        **_mask_webhook_row(row),
    }


@router.delete("/webhooks")
def remove_club_webhook(
    webhook_type: str,
    system_id: Optional[int] = None,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Delete the caller's own club's webhook row if it exists. Idempotent."""
    row = db.exec(
        select(ClubWebhook).where(
            ClubWebhook.club_id == user.club_id,
            ClubWebhook.webhook_type == webhook_type,
            ClubWebhook.system_id == system_id,
        )
    ).first()

    if row is not None:
        db.delete(row)
        db.commit()
        return {"ok": True, "removed": True}

    return {"ok": True, "removed": False}


# ---------------------------------------------------------------------------
# Club schedules — self-service (a club's own super-admin can enable,
# disable, or reschedule any active catalogue system for their own club;
# see POST /admin/platform/clubs/{club_id}/systems above for the
# platform-admin equivalent, used for cross-club management)
# ---------------------------------------------------------------------------

@router.get("/club-systems")
def list_club_systems(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """List the caller's own club's ClubSystem rows, joined with
    SystemConfig for display."""
    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == user.club_id)
    ).all()
    return [
        {
            "system_id": cs.system_id,
            "system_name": sc.name,
            "enabled": cs.enabled,
            "session_day": cs.session_day,
            "session_cadence": cs.session_cadence,
            "cadence_anchor": cs.cadence_anchor,
        }
        for cs, sc in rows
    ]


class ClubSystemScheduleBody(BaseModel):
    system_id: int
    enabled: bool
    session_day: str
    session_cadence: str
    cadence_anchor: Optional[date] = None


@router.post("/club-systems")
def update_club_system_schedule(
    body: ClubSystemScheduleBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Enable/disable/reschedule a catalogue system for the caller's own
    club. Genuine upsert — no longer requires a platform admin to have
    enabled the system for this club first; any club's own super-admin can
    self-service enable or disable any catalogue system. club_id always
    comes from user.club_id, never the request body — same non-negotiable
    rule as scoped() everywhere else. Same shape/validation as the
    platform-admin equivalent, POST /admin/platform/clubs/{club_id}/systems."""
    system = db.get(SystemConfig, body.system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")

    if body.enabled and not system.active:
        raise HTTPException(
            status_code=422, detail="This system is not active in the catalogue and cannot be enabled."
        )

    _validate_schedule_fields(body.session_day, body.session_cadence, body.cadence_anchor)

    existing = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == user.club_id,
            ClubSystem.system_id == body.system_id,
        )
    ).first()

    fields = dict(
        enabled=body.enabled,
        session_day=body.session_day,
        session_cadence=body.session_cadence,
        cadence_anchor=body.cadence_anchor,
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        db.add(existing)
        row = existing
    else:
        row = ClubSystem(club_id=user.club_id, system_id=body.system_id, **fields)
        db.add(row)

    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Platform admin — system catalogue CRUD
#
# Only the platform admin configures what systems exist in the catalogue
# (SystemConfig). Any club's own super-admin can self-service enable/disable
# a catalogue system for their own club — see POST /admin/club-systems below.
# No delete endpoint: a system with real historical signups/pairings/league
# results (string-matched via legacy_system_name, not FK-enforced) could
# never be safely deleted. `active=False` is the platform-wide kill switch
# instead — enforced in POST /admin/club-systems (self-service) and
# POST /admin/platform/clubs/{club_id}/systems (platform-admin) below.
# ---------------------------------------------------------------------------

class SystemConfigCreateBody(BaseModel):
    name: str
    slug: str
    legacy_system_name: str
    uses_points: bool = False
    default_points: Optional[int] = None
    max_points: Optional[int] = None
    vibe_options: list[str] = []
    default_vibe: Optional[str] = None
    uses_scenarios: bool = False
    scenario_options: Optional[list[str]] = None
    default_scenario: Optional[str] = None
    allows_demo: bool = False
    has_intro_prepass: bool = False
    recent_weeks: int = 3
    extended_weeks: int = 6
    escalation_priority: bool = False
    faction_list: Optional[list[str]] = None
    icon_folder: Optional[str] = None
    active: bool = True


class SystemConfigEditBody(BaseModel):
    """Same shape as SystemConfigCreateBody minus slug — slug is immutable
    after creation, since it's used as a stable identifier elsewhere."""
    name: str
    legacy_system_name: str
    uses_points: bool = False
    default_points: Optional[int] = None
    max_points: Optional[int] = None
    vibe_options: list[str] = []
    default_vibe: Optional[str] = None
    uses_scenarios: bool = False
    scenario_options: Optional[list[str]] = None
    default_scenario: Optional[str] = None
    allows_demo: bool = False
    has_intro_prepass: bool = False
    recent_weeks: int = 3
    extended_weeks: int = 6
    escalation_priority: bool = False
    faction_list: Optional[list[str]] = None
    icon_folder: Optional[str] = None
    active: bool = True


def _validate_system_config_fields(
    vibe_options: list[str],
    default_vibe: Optional[str],
    uses_scenarios: bool,
    scenario_options: Optional[list[str]],
    default_scenario: Optional[str],
) -> None:
    if default_vibe not in (vibe_options or []):
        raise HTTPException(
            status_code=422, detail="default_vibe must be one of vibe_options."
        )
    if uses_scenarios and default_scenario not in (scenario_options or []):
        raise HTTPException(
            status_code=422,
            detail="default_scenario must be one of scenario_options when uses_scenarios is true.",
        )


@router.get("/platform/systems")
def list_platform_systems(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """All SystemConfig rows, full fields — the catalogue-management table
    source for the platform admin panel."""
    return db.exec(select(SystemConfig).order_by(SystemConfig.name)).all()


@router.post("/platform/systems")
def create_platform_system(
    body: SystemConfigCreateBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Create a new catalogue system. Replaces seed_systems_config.py as the
    only way this table has ever been touched."""
    if db.exec(select(SystemConfig).where(SystemConfig.slug == body.slug)).first():
        raise HTTPException(status_code=409, detail="A system with this slug already exists.")
    if db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == body.legacy_system_name)
    ).first():
        raise HTTPException(
            status_code=409, detail="A system with this legacy_system_name already exists."
        )

    _validate_system_config_fields(
        body.vibe_options, body.default_vibe, body.uses_scenarios,
        body.scenario_options, body.default_scenario,
    )

    system = SystemConfig(**body.model_dump())
    db.add(system)
    db.commit()
    db.refresh(system)
    return system


@router.post("/platform/systems/{system_id}")
def edit_platform_system(
    system_id: int,
    body: SystemConfigEditBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Full-replace edit of a catalogue system. slug is not accepted here —
    it is immutable after creation."""
    system = db.get(SystemConfig, system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")

    existing = db.exec(
        select(SystemConfig)
        .where(SystemConfig.legacy_system_name == body.legacy_system_name)
        .where(SystemConfig.id != system_id)
    ).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="A system with this legacy_system_name already exists."
        )

    _validate_system_config_fields(
        body.vibe_options, body.default_vibe, body.uses_scenarios,
        body.scenario_options, body.default_scenario,
    )

    for k, v in body.model_dump().items():
        setattr(system, k, v)
    db.add(system)
    db.commit()
    db.refresh(system)
    return system


# ---------------------------------------------------------------------------
# Platform admin — cross-club actions
# ---------------------------------------------------------------------------

@router.get("/platform/clubs")
def list_platform_clubs(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """All clubs, active and inactive both — unlike the public GET /clubs
    (active-only, the /join club-picker's source, untouched by this).
    Platform-admin only: this is the management-table source for the
    platform admin panel."""
    clubs = db.exec(select(Club).order_by(Club.name)).all()
    result = []
    for c in clubs:
        enabled_system_count = len(db.exec(
            scoped(ClubSystem, c.id).where(ClubSystem.enabled == True)
        ).all())
        has_super_admin = db.exec(
            scoped(User, c.id).where(User.is_super_admin == True)
        ).first() is not None
        result.append({
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "active": c.active,
            "leagues_enabled": c.leagues_enabled,
            "timezone": c.timezone,
            "contact_email": c.contact_email,
            "enabled_system_count": enabled_system_count,
            "has_super_admin": has_super_admin,
        })
    return result


class ClubCreateBody(BaseModel):
    name: str
    slug: str
    timezone: str = "Europe/London"
    contact_email: Optional[str] = None
    leagues_enabled: bool = True
    active: bool = True


@router.post("/platform/clubs")
def create_club(
    body: ClubCreateBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Create a new club. Platform-admin only — is_platform_admin is set by
    SQL, never via this API, same pattern as is_super_admin."""
    existing = db.exec(select(Club).where(Club.slug == body.slug)).first()
    if existing:
        raise HTTPException(status_code=409, detail="A club with this slug already exists.")

    club = Club(
        name=body.name,
        slug=body.slug,
        timezone=body.timezone,
        contact_email=body.contact_email,
        leagues_enabled=body.leagues_enabled,
        active=body.active,
    )
    db.add(club)
    db.commit()
    db.refresh(club)
    return club


VALID_CADENCES: frozenset[str] = frozenset({"weekly", "fortnightly"})


def _validate_schedule_fields(session_day: str, session_cadence: str, cadence_anchor: Optional[date]) -> None:
    """Shared by upsert_club_system (platform-admin, enabling a new
    system for a club) and update_club_system_schedule (club
    self-service, editing an already-enabled system's schedule) — same
    fields, same rules, extracted once rather than duplicated."""
    if session_day not in _DAY_NAME_TO_INT:
        raise HTTPException(
            status_code=422,
            detail=f"session_day must be one of: {sorted(_DAY_NAME_TO_INT)}",
        )

    if session_cadence not in VALID_CADENCES:
        raise HTTPException(
            status_code=422,
            detail=f"session_cadence must be one of: {sorted(VALID_CADENCES)}",
        )

    if session_cadence == "fortnightly" and cadence_anchor is None:
        raise HTTPException(
            status_code=422,
            detail="cadence_anchor is required when session_cadence is 'fortnightly'.",
        )
    if session_cadence == "weekly" and cadence_anchor is not None:
        raise HTTPException(
            status_code=422,
            detail="cadence_anchor must not be set when session_cadence is 'weekly'.",
        )

    if cadence_anchor is not None and _DAY_NAME_TO_INT[session_day] != cadence_anchor.weekday():
        raise HTTPException(
            status_code=422,
            detail=(
                f"cadence_anchor ({cadence_anchor.strftime('%A')}) must fall on "
                f"session_day ({session_day})."
            ),
        )


class ClubSystemBody(BaseModel):
    system_id: int
    enabled: bool
    session_day: str
    session_cadence: str
    cadence_anchor: Optional[date] = None


@router.post("/platform/clubs/{club_id}/systems")
def upsert_club_system(
    club_id: int,
    body: ClubSystemBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Enable/configure a system for a club. Upserts on (club_id, system_id).

    session_day/session_cadence/cadence_anchor are read by
    week_logic.py's next_session_date()/is_session_week() — load-bearing
    since the club-schedules handoff, not just informational. A club's
    own super-admin can edit an already-enabled system's schedule via
    GET/POST /admin/club-systems; this endpoint remains the only way to
    enable a *new* system for a club (platform-admin only).
    """
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    system = db.get(SystemConfig, body.system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")

    if body.enabled and not system.active:
        raise HTTPException(
            status_code=422, detail="This system is not active in the catalogue and cannot be enabled."
        )

    _validate_schedule_fields(body.session_day, body.session_cadence, body.cadence_anchor)

    existing = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == club_id,
            ClubSystem.system_id == body.system_id,
        )
    ).first()

    fields = dict(
        enabled=body.enabled,
        session_day=body.session_day,
        session_cadence=body.session_cadence,
        cadence_anchor=body.cadence_anchor,
    )
    if existing:
        for k, v in fields.items():
            setattr(existing, k, v)
        db.add(existing)
        row = existing
    else:
        row = ClubSystem(club_id=club_id, system_id=body.system_id, **fields)
        db.add(row)

    db.commit()
    db.refresh(row)
    return row


@router.get("/platform/clubs/{club_id}/systems")
def list_platform_club_systems(
    club_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """That specific club's ClubSystem rows joined to SystemConfig —
    platform-admin view of any club, same join shape the self-service
    GET /admin/club-systems already uses for the caller's own club."""
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id)
    ).all()
    return [
        {
            "system_id": cs.system_id,
            "system_name": sc.name,
            "enabled": cs.enabled,
            "session_day": cs.session_day,
            "session_cadence": cs.session_cadence,
            "cadence_anchor": cs.cadence_anchor,
        }
        for cs, sc in rows
    ]


class AppointSuperAdminBody(BaseModel):
    user_id: int


def _platform_user_row(u: User) -> dict:
    return {
        "id": u.id,
        "discord_name": u.discord_name,
        "club_id": u.club_id,
        "is_super_admin": u.is_super_admin,
    }


@router.post("/platform/clubs/{club_id}/super-admins")
def appoint_club_super_admin(
    club_id: int,
    body: AppointSuperAdminBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Appoint a club's first super-admin.

    Closes the bootstrap gap left by POST /admin/platform/clubs: a brand-new
    club has no super-admin yet, and every existing /admin/roles endpoint
    requires require_super_admin (club-scoped) to call it. Idempotent —
    calling again on an already-appointed user is a no-op.
    """
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    target = db.get(User, body.user_id)
    if target is None or target.club_id != club_id:
        raise HTTPException(status_code=404, detail="User not found.")

    if not target.is_super_admin:
        target.is_super_admin = True
        db.add(target)
        db.commit()
        db.refresh(target)

    return _platform_user_row(target)


@router.get("/platform/clubs/{club_id}/super-admins")
def list_platform_club_super_admins(
    club_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """That club's current super-admins — platform-admin view of any
    club, mirrors the super_admins half of the self-service
    GET /admin/roles (which is scoped to the caller's own club only)."""
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    rows = db.exec(
        scoped(User, club_id).where(User.is_super_admin == True).order_by(User.discord_name)
    ).all()
    result = []
    for sa in rows:
        player_name = None
        if sa.player_id:
            p = db.get(Player, sa.player_id)
            player_name = p.name if p else None
        result.append({
            "user_id": sa.id,
            "discord_name": sa.discord_name,
            "player_name": player_name,
        })
    return result


@router.get("/platform/clubs/{club_id}/grantable-users")
def list_platform_club_grantable_users(
    club_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """That club's active users with a linked player — platform-admin
    view of any club, the appoint-super-admin picker source. Mirrors the
    self-service GET /admin/grantable-users' exact logic, just not
    restricted to the caller's own club."""
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    users = db.exec(
        scoped(User, club_id).where(User.player_id.isnot(None))
    ).all()

    result = []
    for u in users:
        p = db.get(Player, u.player_id)
        if p and p.active:
            result.append({
                "id": u.id,
                "discord_name": u.discord_name,
                "player_name": p.name,
            })

    result.sort(key=lambda x: x["player_name"].lower())
    return result


@router.delete("/platform/clubs/{club_id}/super-admins/{user_id}")
def remove_club_super_admin(
    club_id: int,
    user_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Revoke a club's super-admin status. Idempotent."""
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    target = db.get(User, user_id)
    if target is None or target.club_id != club_id:
        raise HTTPException(status_code=404, detail="User not found.")

    removed = target.is_super_admin
    if removed:
        target.is_super_admin = False
        db.add(target)
        db.commit()

    return {"ok": True, "removed": removed}


class ClubActiveBody(BaseModel):
    active: bool


@router.post("/platform/clubs/{club_id}/active")
def set_club_active(
    club_id: int,
    body: ClubActiveBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Set a club's active flag directly. Idempotent — calling with the
    current value is a no-op write, still returns 200 with current
    state. Replaces the by-SQL-only pattern previously used for
    Yorkshire's own activation."""
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    if club.active != body.active:
        club.active = body.active
        db.add(club)
        db.commit()
        db.refresh(club)

    return {"id": club.id, "slug": club.slug, "active": club.active}
