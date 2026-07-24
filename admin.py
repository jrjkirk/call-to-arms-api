"""Admin endpoints: role management and scoped access.

Permission model:
- Super-admin: user.is_super_admin == True. Set by SQL, never via this API.
  Can do everything and is the only role that can appoint/remove scope admins.
- Scope admin: a row in admin_roles (user_id, scope). One row per scope held.
"""
import os
import re
from datetime import date, datetime
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    admin_scopes,
    club_runnable_scopes,
    current_user,
    get_session,
    require_platform_admin,
    require_super_admin,
    require_user,
    valid_scopes,
)
from database import scoped, system_setting_slug as _slug, get_setting as _get_setting, upsert_setting as _upsert_setting, log_audit
from league import (
    VALID_GAME_TYPES,
    VALID_PAINTING,
    VALID_RESULTS,
    _current_season_id,
    _get_league_config,
    _normalise_optional,
    _recalculate_ratings,
    _resolve_system_id,
    _season_champion,
)
from models import AdminRole, AuditLogEntry, Club, ClubEvent, ClubRequest, ClubSetting, ClubSystem, ClubWebhook, LeagueConfig, LeagueResult, LeagueSeason, Mission, PairingBlock, Pairing, PairingConfig, Player, PlatformBanner, PublishState, ScheduledJobRun, Signup, SystemConfig, TableBookingConfig, TableBookingNotification, User
import storage
from services import player_titles, set_player_titles
import call_to_arms_content as cta_content
from pairings_engine import generate, _get_pairing_config, summarize_pairings
from systems import factions_for, icon_folder_for
from table_booking import compute_table_booking, maybe_send_table_booking, render_table_booking_email, send_table_booking_notification
from signups import (
    CANONICAL_VIBES,
    EXPERIENCE_OPTIONS,
    _effective_vibe_config,
    _get_system_config,
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
        log_audit(db, user, "role.grant", "user", target.id, f"{target.discord_name!r} -> {body.scope!r}")
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
        target = db.get(User, user_id)
        target_name = target.discord_name if target else str(user_id)
        db.delete(row)
        log_audit(db, user, "role.revoke", "user", user_id, f"{target_name!r} -> {scope!r}")
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
        rows = db.exec(scoped(Player, user.club_id).where(Player.id.in_(player_ids))).all()
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
    """Recent game history for a scope. 403 unless caller holds that scope.
    (League game history lives in GET /admin/league/results now — there is
    no separate "League" scope; every scope here is a system.)"""
    if scope not in valid_scopes(db):
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if scope not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")

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
        signup_rows = db.exec(scoped(Signup, user.club_id).where(Signup.id.in_(signup_ids))).all()
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


def _pts_show(a_su: Optional[Signup], b_su: Optional[Signup], uses_points: bool) -> Optional[str]:
    if not uses_points:
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
    uses_points: bool,
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
        "points": _pts_show(a_su, b_su, uses_points),
        "prearranged": prearranged,
    }


def _pairs_of(rows) -> list:
    """(a_signup_id, b_signup_id|None) tuples from a mixed list of Pairing rows
    and preview dicts — the shape pairings_engine.summarize_pairings expects."""
    out = []
    for r in rows:
        if isinstance(r, Pairing):
            out.append((r.a_signup_id, r.b_signup_id))
        else:
            out.append((r.get("a_signup_id"), r.get("b_signup_id")))
    return out


def _collect_signups_for_rows(rows, db: Session, club_id: int) -> dict:
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
    rows_q = db.exec(scoped(Signup, club_id).where(Signup.id.in_(ids))).all()
    return {s.id: s for s in rows_q}


def _pairing_rows_to_display(pairings: list, signups_by_id: dict, uses_points: bool) -> list:
    result = []
    for p in pairings:
        a_faction = p.a_faction or (signups_by_id.get(p.a_signup_id, None) and signups_by_id[p.a_signup_id].faction)
        b_faction = None
        if p.b_signup_id:
            b_faction = p.b_faction or (signups_by_id.get(p.b_signup_id, None) and signups_by_id[p.b_signup_id].faction)
        result.append(_build_display_row(
            p.id, p.a_signup_id, p.b_signup_id,
            a_faction, b_faction,
            p.prearranged, signups_by_id, uses_points,
        ))
    return result


def _dicts_to_display(dicts: list, signups_by_id: dict, uses_points: bool) -> list:
    return [
        _build_display_row(
            None,
            d["a_signup_id"], d.get("b_signup_id"),
            d.get("a_faction"), d.get("b_faction"),
            False, signups_by_id, uses_points,
        )
        for d in dicts
    ]

_VALID_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_TIME_RE = re.compile(r"^(2[0-3]|[01]\d):[0-5]\d$")


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


class CallToArmsSettingsBody(BaseModel):
    system: str
    enabled: bool
    days_before: int
    time: str
    template: Optional[str] = None
    image_mode: Optional[str] = None
    image_url: Optional[str] = None


@router.get("/call-to-arms-settings")
def get_call_to_arms_settings(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Per-club, per-system call-to-arms schedule. Mirrors
    get_auto_pairings_settings, but scheduled as N days before the club's
    session day rather than on an absolute weekday, so it self-adjusts if
    the club changes its ClubSystem.session_day."""
    _require_system_scope(system, user, db)
    slug = _slug(system)
    enabled_str = (_get_setting(db, user.club_id, f"call_to_arms_{slug}_enabled", "false") or "false").lower()
    days_before_str = _get_setting(db, user.club_id, f"call_to_arms_{slug}_days_before", "3") or "3"
    template_override = _get_setting(db, user.club_id, f"call_to_arms_{slug}_template")
    default_template = cta_content.default_template(system)
    image_mode, image_url = cta_content.parse_image_setting(
        _get_setting(db, user.club_id, f"call_to_arms_{slug}_image")
    )
    # A club-system supports a mission image (and the scenario tokens) if it
    # has the DB mission pool enabled, or via the legacy hardcoded fallback.
    config = _get_system_config(db, system)
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first() if config else None
    has_missions = bool(cs and cs.missions_enabled) or (system in cta_content.SCENARIO_DATA)
    return {
        "enabled": enabled_str == "true",
        "days_before": int(days_before_str),
        "time": _get_setting(db, user.club_id, f"call_to_arms_{slug}_time", "12:00") or "12:00",
        "last_week": _get_setting(db, user.club_id, f"call_to_arms_{slug}_last_week"),
        "template": template_override if template_override else default_template,
        "default_template": default_template,
        "tokens": cta_content.available_tokens(system, has_missions),
        "image_mode": image_mode,
        "image_url": image_url or "",
        "supports_mission_image": has_missions,
    }


@router.post("/call-to-arms-settings")
def post_call_to_arms_settings(
    body: CallToArmsSettingsBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    _require_system_scope(body.system, user, db)
    if not 0 <= body.days_before <= 14:
        raise HTTPException(status_code=422, detail="days_before must be between 0 and 14")
    if not _TIME_RE.match(body.time):
        raise HTTPException(status_code=422, detail="time must match HH:MM (00-23 / 00-59)")
    slug = _slug(body.system)
    _upsert_setting(db, user.club_id, f"call_to_arms_{slug}_enabled", "true" if body.enabled else "false")
    _upsert_setting(db, user.club_id, f"call_to_arms_{slug}_days_before", str(body.days_before))
    _upsert_setting(db, user.club_id, f"call_to_arms_{slug}_time", body.time)
    if body.template is not None:
        # Store an override only when it differs from the default; an empty
        # or default-equal template clears the override so the club tracks
        # the system default going forward.
        key = f"call_to_arms_{slug}_template"
        if body.template.strip() == "" or body.template == cta_content.default_template(body.system):
            existing = db.get(ClubSetting, (user.club_id, key))
            if existing is not None:
                db.delete(existing)
        else:
            _upsert_setting(db, user.club_id, key, body.template)
    if body.image_mode is not None:
        if body.image_mode not in cta_content.IMAGE_MODES:
            raise HTTPException(status_code=422, detail=f"image_mode must be one of {list(cta_content.IMAGE_MODES)}")
        if body.image_mode == "custom":
            url = (body.image_url or "").strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(status_code=422, detail="A custom image requires a valid http(s) URL.")
        key = f"call_to_arms_{slug}_image"
        stored = cta_content.image_setting_value(body.image_mode, body.image_url)
        if stored is None:
            existing = db.get(ClubSetting, (user.club_id, key))
            if existing is not None:
                db.delete(existing)
        else:
            _upsert_setting(db, user.club_id, key, stored)
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
    signups_by_id = _collect_signups_for_rows(all_rows, db, user.club_id)

    config = _get_system_config(db, body.system)
    uses_points = config.uses_points if config else (body.system != "Kill Team")
    display = _pairing_rows_to_display(prearranged, signups_by_id, uses_points) + \
              _dicts_to_display(proposed_dicts, signups_by_id, uses_points)

    summary = summarize_pairings(db, body.system, body.week, user.club_id, _pairs_of(all_rows))
    return {"rows": display, "preview": True, "summary": summary}


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

    # Include any prearranged rows so the summary reflects the whole week, not
    # just the freshly-generated pairs.
    prearranged = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.week == body.week)
        .where(Pairing.system == body.system)
        .where(Pairing.prearranged == True)
    ).all()

    signups_by_id = _collect_signups_for_rows(new_pairings, db, user.club_id)
    config = _get_system_config(db, body.system)
    uses_points = config.uses_points if config else (body.system != "Kill Team")
    display = _pairing_rows_to_display(new_pairings, signups_by_id, uses_points)
    summary = summarize_pairings(
        db, body.system, body.week, user.club_id, _pairs_of(list(new_pairings) + list(prearranged))
    )
    return {"rows": display, "summary": summary}


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

    signups_by_id = _collect_signups_for_rows(rows, db, user.club_id)
    config = _get_system_config(db, system)
    uses_points = config.uses_points if config else (system != "Kill Team")
    display = _pairing_rows_to_display(rows, signups_by_id, uses_points)
    summary = summarize_pairings(db, system, week, user.club_id, _pairs_of(rows)) if rows else None
    return {"rows": display, "published": published, "summary": summary}


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
    if body.published:
        maybe_send_table_booking(db, user.club_id, body.system, body.week)
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


def _system_config(db: Session, club_id: int, system: str) -> dict:
    """Per-system field visibility config for the admin signup editor. Sourced
    from the SystemConfig catalogue (+ the caller's own ClubSystem vibe
    override, if any — same merge as the public signup form uses via
    _effective_vibe_config) rather than hardcoded per-system-name literals,
    so a club's own vibe customization is honored here too.

    show_standby now comes from the SystemConfig.uses_standby catalogue
    capability (backfilled to The Old World, its original only home)."""
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    vibe_options, _default_vibe = _effective_vibe_config(db, club_id, config)
    return {
        "show_points": config.uses_points,
        "default_points": config.default_points,
        "show_scenario": config.uses_scenarios,
        "show_standby": config.uses_standby,
        "show_can_demo": config.allows_demo,
        "vibe_options": vibe_options,
        "vibe_fixed": vibe_options[0] if len(vibe_options) == 1 else None,
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
        "config": _system_config(db, user.club_id, system),
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
    config = _get_system_config(db, su.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    uses_points, uses_scenarios, allows_demo = config.uses_points, config.uses_scenarios, config.allows_demo
    vibe_options, default_vibe = _effective_vibe_config(db, user.club_id, config)

    if "faction" in provided:
        f = body.faction
        su.faction = None if f in (None, "", "— None —") else f

    if "points" in provided:
        if not uses_points or body.points is None:
            su.points = None
        else:
            su.points = max(0, min(int(body.points), 10000))

    if "eta" in provided:
        su.eta = (body.eta or "").strip() or None

    if "experience" in provided:
        su.experience = body.experience if body.experience in EXPERIENCE_OPTIONS else "New"

    if "vibe" in provided:
        su.vibe = body.vibe if body.vibe in vibe_options else default_vibe

    if "standby_ok" in provided:
        su.standby_ok = bool(body.standby_ok) if body.standby_ok is not None else False

    if "scenario" in provided:
        if not uses_scenarios:
            su.scenario = None
        else:
            s = body.scenario
            su.scenario = None if s in (None, "", "— None —") else s

    if "can_demo" in provided:
        su.can_demo = bool(body.can_demo) if (allows_demo and body.can_demo is not None) else False

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
    config = _get_system_config(db, body.system)
    if config is None:
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

    uses_points, uses_scenarios, allows_demo = config.uses_points, config.uses_scenarios, config.allows_demo
    default_points, max_points = config.default_points, config.max_points
    vibe_options, default_vibe = _effective_vibe_config(db, user.club_id, config)

    faction = body.faction
    if faction in (None, "", "— None —"):
        faction = None

    experience = body.experience if body.experience in EXPERIENCE_OPTIONS else "New"
    eta = (body.eta or "").strip() or None

    vibe = body.vibe if body.vibe in vibe_options else default_vibe
    points = None if not uses_points else max(0, min(int(body.points or default_points), max_points))
    if uses_scenarios:
        s = body.scenario
        scenario = None if s in (None, "", "— None —") else s
    else:
        scenario = None
    can_demo = bool(body.can_demo) if allows_demo else False
    standby_ok = bool(body.standby_ok) if config.uses_standby else False

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
    system: str,
    season_id: Optional[int] = None,
    user: User = Depends(require_user),
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
    user: User = Depends(require_user),
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
    user: User = Depends(require_user),
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
    user: User = Depends(require_user),
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

        # Idempotency: skip if this exact pairing's players already have a result
        # logged for this week/season (guards against a double-submit).
        already = db.exec(
            scoped(LeagueResult, user.club_id)
            .where(LeagueResult.system_id == system_id)
            .where(LeagueResult.season_id == season_id)
            .where(LeagueResult.result_date == result_date)
            .where(LeagueResult.player_1_id == a_su.player_id)
            .where(LeagueResult.player_2_id == b_su.player_id)
        ).first()
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
        "painting_fully_bonus": cfg.painting_fully_bonus,
        "painting_partial_bonus": cfg.painting_partial_bonus,
        "points_win": cfg.points_win,
        "points_draw": cfg.points_draw,
        "points_loss": cfg.points_loss,
        "winloss_use_painting": cfg.winloss_use_painting,
    }


@router.get("/league-config")
def get_league_config(
    system: str,
    user: User = Depends(require_user),
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
    user: User = Depends(require_user),
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
    painting_fully_bonus: float = 3.0
    painting_partial_bonus: float = 1.0
    points_win: float = 3.0
    points_draw: float = 1.0
    points_loss: float = 0.0
    winloss_use_painting: bool = False


@router.post("/league-config")
def save_league_config(
    body: LeagueConfigBody,
    user: User = Depends(require_user),
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


# ---------------------------------------------------------------------------
# Pairing weighting config — per-(club, system), self-service by that
# system's admin (same _require_system_scope gate as league config above).
# ---------------------------------------------------------------------------

def _pairing_config_row(cfg: PairingConfig) -> dict:
    return {
        "weight_mirror": cfg.weight_mirror,
        "weight_rematch": cfg.weight_rematch,
        "weight_vibe": cfg.weight_vibe,
        "weight_experience": cfg.weight_experience,
        "weight_eta": cfg.weight_eta,
        "weight_scenario": cfg.weight_scenario,
        "weight_points": cfg.weight_points,
        "recent_weeks": cfg.recent_weeks,
        "extended_weeks": cfg.extended_weeks,
    }


@router.get("/pairing-config")
def get_pairing_config(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """This club's pairing weighting config for one system. Returns the
    unsaved-defaults shape if the club hasn't customised it yet — same
    "defaults until saved" convention as league config."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cfg = _get_pairing_config(db, user.club_id, config.id)
    return {
        "uses_scenarios": config.uses_scenarios,
        "uses_points": config.uses_points,
        # Platform defaults shown as placeholders when the club hasn't overridden
        # the rematch windows.
        "default_recent_weeks": config.recent_weeks,
        "default_extended_weeks": config.extended_weeks,
        **_pairing_config_row(cfg),
    }


class PairingConfigBody(BaseModel):
    system: str
    weight_mirror: float = 5.0
    weight_rematch: float = 3.0
    weight_vibe: float = 1.5
    weight_experience: float = 0.8
    weight_eta: float = 0.4
    weight_scenario: float = 0.2
    weight_points: float = 0.1
    # null = use the platform SystemConfig default window.
    recent_weeks: Optional[int] = None
    extended_weeks: Optional[int] = None


@router.post("/pairing-config")
def save_pairing_config(
    body: PairingConfigBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Upsert this club's pairing weighting config for one system. Takes
    effect on the next pairings preview/generate call — no replay needed
    (unlike league config, weighting only affects future matchmaking)."""
    _require_system_scope(body.system, user, db)
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    cfg = db.exec(
        select(PairingConfig).where(
            PairingConfig.club_id == user.club_id, PairingConfig.system_id == config.id
        )
    ).first()
    if cfg is None:
        cfg = PairingConfig(club_id=user.club_id, system_id=config.id)
    for field in PairingConfigBody.model_fields:
        if field == "system":
            continue
        setattr(cfg, field, getattr(body, field))
    db.add(cfg)
    db.commit()
    return _pairing_config_row(cfg)


# ---------------------------------------------------------------------------
# Table booking — per-(club, system) venue table-booking email config,
# self-service by that system's own admin (same _require_system_scope gate
# as missions/league config above). Slab 1: config CRUD only, no sending yet.
# ---------------------------------------------------------------------------

VALID_TABLE_BOOKING_SEND_MODES = {"on_publish", "cutoff"}


def _table_booking_config_row(cfg: Optional[TableBookingConfig]) -> dict:
    if cfg is None:
        return {
            "venue_name": None,
            "venue_email": "",
            "cc_emails": [],
            "players_per_table": 2,
            "include_player_names": True,
            "send_mode": "on_publish",
            "cutoff_day": None,
            "cutoff_time": None,
            "subject_template": None,
            "notes": None,
        }
    return {
        "venue_name": cfg.venue_name,
        "venue_email": cfg.venue_email,
        "cc_emails": cfg.cc_emails or [],
        "players_per_table": cfg.players_per_table,
        "include_player_names": cfg.include_player_names,
        "send_mode": cfg.send_mode,
        "cutoff_day": cfg.cutoff_day,
        "cutoff_time": cfg.cutoff_time,
        "subject_template": cfg.subject_template,
        "notes": cfg.notes,
    }


@router.get("/table-booking-settings")
def get_table_booking_settings(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """This club's table_booking_enabled flag + venue config for one system.
    Config returns the unsaved-defaults shape if the club hasn't set it up
    yet — same "defaults until saved" convention as league-config."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    cfg = db.exec(
        select(TableBookingConfig).where(
            TableBookingConfig.club_id == user.club_id,
            TableBookingConfig.system_id == config.id,
        )
    ).first()
    return {
        "table_booking_enabled": bool(cs and cs.table_booking_enabled),
        **_table_booking_config_row(cfg),
    }


class TableBookingSettingsBody(BaseModel):
    system: str
    table_booking_enabled: bool


@router.post("/table-booking-settings")
def update_table_booking_settings(
    body: TableBookingSettingsBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Enable/disable venue table-booking emails for one system (mirrors
    POST /admin/league-settings). Disabling does not delete the venue
    config, just stops sending."""
    _require_system_scope(body.system, user, db)
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")
    cs.table_booking_enabled = body.table_booking_enabled
    db.add(cs)
    db.commit()
    return {"ok": True}


class TableBookingConfigBody(BaseModel):
    system: str
    venue_name: Optional[str] = None
    venue_email: str
    cc_emails: list[str] = []
    players_per_table: int = 2
    include_player_names: bool = True
    send_mode: str = "on_publish"
    cutoff_day: Optional[str] = None
    cutoff_time: Optional[str] = None
    subject_template: Optional[str] = None
    notes: Optional[str] = None


@router.post("/table-booking-config")
def save_table_booking_config(
    body: TableBookingConfigBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Upsert this club's venue table-booking config for one system."""
    _require_system_scope(body.system, user, db)
    if body.send_mode not in VALID_TABLE_BOOKING_SEND_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"send_mode must be one of {sorted(VALID_TABLE_BOOKING_SEND_MODES)}.",
        )
    if "@" not in body.venue_email:
        raise HTTPException(status_code=422, detail="venue_email must be a valid email address.")
    if body.players_per_table < 1:
        raise HTTPException(status_code=422, detail="players_per_table must be at least 1.")
    if body.send_mode == "cutoff" and not (body.cutoff_day and body.cutoff_time):
        raise HTTPException(
            status_code=422,
            detail="cutoff_day and cutoff_time are required when send_mode is 'cutoff'.",
        )
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    cfg = db.exec(
        select(TableBookingConfig).where(
            TableBookingConfig.club_id == user.club_id,
            TableBookingConfig.system_id == config.id,
        )
    ).first()
    if cfg is None:
        cfg = TableBookingConfig(club_id=user.club_id, system_id=config.id, venue_email=body.venue_email)
    for field in TableBookingConfigBody.model_fields:
        if field == "system":
            continue
        setattr(cfg, field, getattr(body, field))
    cfg.updated_at = datetime.utcnow()
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return _table_booking_config_row(cfg)


def _table_booking_config_or_404(db: Session, club_id: int, system: str) -> tuple[SystemConfig, TableBookingConfig]:
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cfg = db.exec(
        select(TableBookingConfig).where(
            TableBookingConfig.club_id == club_id,
            TableBookingConfig.system_id == config.id,
        )
    ).first()
    if cfg is None:
        raise HTTPException(status_code=404, detail="Table booking is not configured for this system yet.")
    return config, cfg


class TableBookingPreviewBody(BaseModel):
    system: str
    week: str


@router.post("/table-booking/preview")
def preview_table_booking(
    body: TableBookingPreviewBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Dry run: compute tables/headcount and render the email for one
    week, without sending or writing a notification row."""
    _require_system_scope(body.system, user, db)
    config, cfg = _table_booking_config_or_404(db, user.club_id, body.system)

    data = compute_table_booking(db, user.club_id, body.system, body.week, cfg.players_per_table)
    subject, html = render_table_booking_email(
        cfg, body.system, body.week, data["tables"], data["headcount"], data["player_names"]
    )
    already_sent = db.exec(
        select(TableBookingNotification).where(
            TableBookingNotification.club_id == user.club_id,
            TableBookingNotification.system_id == config.id,
            TableBookingNotification.week == body.week,
            TableBookingNotification.status == "sent",
        )
    ).first() is not None

    return {**data, "subject": subject, "html": html, "already_sent": already_sent}


@router.post("/table-booking/send")
def send_table_booking(
    body: TableBookingPreviewBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Explicit manual send — always executes (allow_duplicate=True), unlike
    the automatic on_publish/cutoff triggers, which are idempotent per week.
    A super-admin clicking Send is a deliberate action, not a re-triggered
    automation, so it's allowed to resend a week that already went out."""
    _require_system_scope(body.system, user, db)
    config, cfg = _table_booking_config_or_404(db, user.club_id, body.system)

    notif = send_table_booking_notification(
        db, user.club_id, config.id, body.system, body.week, cfg, allow_duplicate=True,
    )
    return {
        "status": notif.status,
        "error": notif.error,
        "tables": notif.tables,
        "headcount": notif.headcount,
        "sent_at": notif.sent_at.isoformat(),
    }


@router.get("/table-booking-history")
def table_booking_history(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """The last 10 send attempts (success or failure) for this club/system,
    newest first — powers the admin oversight "recent sends" log."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    rows = db.exec(
        select(TableBookingNotification)
        .where(TableBookingNotification.club_id == user.club_id)
        .where(TableBookingNotification.system_id == config.id)
        .order_by(TableBookingNotification.sent_at.desc())
        .limit(10)
    ).all()
    return [
        {
            "week": r.week, "tables": r.tables, "headcount": r.headcount,
            "status": r.status, "error": r.error, "sent_at": r.sent_at.isoformat(),
        }
        for r in rows
    ]


def _league_season_row(db: Session, s: LeagueSeason, current_id: Optional[int]) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "start_date": s.start_date.isoformat(),
        "end_date": s.end_date.isoformat() if s.end_date else None,
        "current": s.id == current_id,
        "champion": _season_champion(db, s.club_id, s.system_id, s.id),
    }


@router.get("/league-seasons")
def list_league_seasons(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """This club's seasons for one system's league, newest first, each
    flagged whether it's the current one (see league._current_season_id) and
    carrying its champion (see league._season_champion)."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    seasons = db.exec(
        select(LeagueSeason)
        .where(LeagueSeason.club_id == user.club_id, LeagueSeason.system_id == config.id)
        .order_by(LeagueSeason.start_date.desc())
    ).all()
    current_id = _current_season_id(db, user.club_id, config.id)
    return [_league_season_row(db, s, current_id) for s in seasons]


class LeagueSeasonCreateBody(BaseModel):
    system: str
    name: str
    start_date: date
    end_date: Optional[date] = None


@router.post("/league-seasons")
def create_league_season(
    body: LeagueSeasonCreateBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Start a new season for one system's league. Ratings reset — the new
    season begins empty at LeagueConfig.starting_rating; results/ratings
    already tagged to prior seasons are untouched and stay archived.

    If an existing season for this (club, system) is open-ended (end_date
    NULL) and starts before this one, it's automatically closed the day
    before this season's start_date — "creating a new season closes the
    old one" (no separate close step needed)."""
    _require_system_scope(body.system, user, db)
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    if body.end_date is not None and body.end_date < body.start_date:
        raise HTTPException(status_code=422, detail="end_date must not be before start_date.")

    open_seasons = db.exec(
        select(LeagueSeason).where(
            LeagueSeason.club_id == user.club_id,
            LeagueSeason.system_id == config.id,
            LeagueSeason.end_date.is_(None),
        )
    ).all()
    for prior in open_seasons:
        if prior.start_date < body.start_date:
            from datetime import timedelta
            prior.end_date = body.start_date - timedelta(days=1)
            db.add(prior)

    season = LeagueSeason(
        club_id=user.club_id, system_id=config.id,
        name=body.name, start_date=body.start_date, end_date=body.end_date,
    )
    db.add(season)
    db.commit()
    db.refresh(season)
    current_id = _current_season_id(db, user.club_id, config.id)
    return _league_season_row(db, season, current_id)


# ---------------------------------------------------------------------------
# Club webhooks — self-service, per-system scope
# ---------------------------------------------------------------------------

# Shown (and creatable) for every system the club has enabled.
WEBHOOK_TYPES_PER_SYSTEM: tuple[str, ...] = ("signup", "pairings", "call_to_arms")
# Shown (and creatable) only for systems that ALSO have their league enabled
# (ClubSystem.league_enabled) — these were club-wide (system_id always NULL)
# before leagues themselves became per-system; a club running two leagues
# couldn't route their result/rankings/achievement posts to different
# channels. Migrated existing club-level rows to their club's single
# league-enabled system — see migrations/split_league_webhooks_per_system.py.
WEBHOOK_TYPES_LEAGUE: tuple[str, ...] = ("league_result", "achievement", "league_rankings")
ALL_WEBHOOK_TYPES: frozenset[str] = frozenset(WEBHOOK_TYPES_PER_SYSTEM + WEBHOOK_TYPES_LEAGUE)


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
    """Webhook grid for the caller's own club, scoped to what the club
    actually runs: per-system webhooks (signup/pairings/call_to_arms) appear
    for every system this club has enabled (not the whole catalogue); league
    webhooks (league_result/achievement/league_rankings) appear only for
    systems that also have their league enabled. Each listed (type, system)
    appears whether or not a row exists yet, so the UI shows what's
    configurable, not just what's set."""
    club_systems = db.exec(scoped(ClubSystem, user.club_id)).all()
    enabled_system_ids = {cs.system_id for cs in club_systems if cs.enabled}
    league_system_ids = {cs.system_id for cs in club_systems if cs.league_enabled}
    systems = [
        s for s in db.exec(select(SystemConfig).where(SystemConfig.active == True)).all()
        if s.id in enabled_system_ids
    ]

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
    for webhook_type in WEBHOOK_TYPES_LEAGUE:
        for system in systems:
            if system.id not in league_system_ids:
                continue
            row = existing_by_key.get((webhook_type, system.id))
            grid.append({
                "webhook_type": webhook_type,
                "system_id": system.id,
                "system_name": system.name,
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

    if body.webhook_type in WEBHOOK_TYPES_PER_SYSTEM or body.webhook_type in WEBHOOK_TYPES_LEAGUE:
        if body.system_id is None:
            raise HTTPException(
                status_code=422,
                detail=f"system_id is required for webhook_type={body.webhook_type!r}.",
            )
        system = db.get(SystemConfig, body.system_id)
        if system is None:
            raise HTTPException(status_code=404, detail="System not found.")
        club_system = db.exec(
            scoped(ClubSystem, user.club_id)
            .where(ClubSystem.system_id == body.system_id)
            .where(ClubSystem.enabled == True)
        ).first()
        if club_system is None:
            raise HTTPException(
                status_code=422,
                detail="This system is not enabled for your club.",
            )
        if body.webhook_type in WEBHOOK_TYPES_LEAGUE and not club_system.league_enabled:
            raise HTTPException(
                status_code=422,
                detail="League and achievement webhooks require this system's league to be enabled.",
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
            "session_start_time": cs.session_start_time,
            # Per-club vibe override (null = falls back to the catalogue
            # default). The catalogue default is surfaced too so the edit
            # form can pre-fill / show what "unset" resolves to.
            "vibe_options": cs.vibe_options,
            "default_vibe": cs.default_vibe,
            "default_vibe_options": sc.vibe_options,
            "default_default_vibe": sc.default_vibe,
        }
        for cs, sc in rows
    ]


class ClubSystemScheduleBody(BaseModel):
    system_id: int
    enabled: bool
    session_day: str
    session_cadence: str
    cadence_anchor: Optional[date] = None
    session_start_time: Optional[str] = None
    # Per-club vibe config. Omit (None) to leave unchanged; an empty list
    # clears the override (falls back to the catalogue default).
    vibe_options: Optional[list] = None
    default_vibe: Optional[str] = None


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

    _validate_schedule_fields(body.session_day, body.session_cadence, body.cadence_anchor, body.session_start_time)

    # Vibe config: omitted (None) leaves it unchanged; [] clears the override
    # (falls back to the catalogue default); a list validates against the
    # canonical palette and stores as this club's override.
    vibe_fields: dict = {}
    if body.vibe_options is not None:
        invalid = [v for v in body.vibe_options if v not in CANONICAL_VIBES]
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid vibe(s): {invalid}. Must be from {CANONICAL_VIBES}.",
            )
        vibe_options = body.vibe_options or None
        default_vibe = None
        if vibe_options:
            default_vibe = body.default_vibe if body.default_vibe in vibe_options else vibe_options[0]
        vibe_fields = {"vibe_options": vibe_options, "default_vibe": default_vibe}

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
        session_start_time=body.session_start_time,
        **vibe_fields,
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
    uses_standby: bool = False
    has_intro_prepass: bool = False
    has_league: bool = False
    recent_weeks: int = 3
    extended_weeks: int = 6
    # faction_list / icon_folder are NOT accepted here. A system's factions
    # and icon directory are rules that live in versioned code (systems/),
    # not editable catalogue data. Following the same convention as `slug`
    # immutability-on-edit, they're simply omitted from the request body so
    # Pydantic silently ignores them if a client sends them.
    active: bool = True


class SystemConfigEditBody(BaseModel):
    """Same shape as SystemConfigCreateBody minus slug — slug is immutable
    after creation, since it's used as a stable identifier elsewhere.
    faction_list / icon_folder are likewise omitted: they are code-owned
    rules (systems/), never editable, and silently ignored if sent."""
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
    uses_standby: bool = False
    has_intro_prepass: bool = False
    has_league: bool = False
    recent_weeks: int = 3
    extended_weeks: int = 6
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
    source for the platform admin panel.

    faction_list / icon_folder are overridden from the hardcoded per-system
    modules (systems/), not read from the DB columns, so the panel shows the
    real code-owned ruleset (and never presents it as editable data)."""
    rows = db.exec(select(SystemConfig).order_by(SystemConfig.name)).all()
    result = []
    for r in rows:
        row = r.model_dump()
        row["faction_list"] = factions_for(r.legacy_system_name)
        row["icon_folder"] = icon_folder_for(r.legacy_system_name)
        result.append(row)
    return result


@router.post("/platform/systems")
def create_platform_system(
    body: SystemConfigCreateBody,
    user: User = Depends(require_platform_admin),
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
    db.flush()
    log_audit(db, user, "system.create", "system", system.id, f"{system.name!r} (slug={system.slug})")
    db.commit()
    db.refresh(system)
    return system


@router.post("/platform/systems/{system_id}")
def edit_platform_system(
    system_id: int,
    body: SystemConfigEditBody,
    user: User = Depends(require_platform_admin),
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
    log_audit(db, user, "system.update", "system", system.id, system.name)
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
    active: bool = True


@router.post("/platform/clubs")
def create_club(
    body: ClubCreateBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Create a new club. Platform-admin only — is_platform_admin is set by
    SQL, never via this API, same pattern as is_super_admin.

    Slug is validated the same way update_club validates it (hostname-safe
    format, lowercased) — previously this only checked uniqueness, not
    format, even though the slug is the club's <slug>.calltoarms.app
    subdomain identifier from the moment it's created."""
    slug = body.slug.strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=422,
            detail="Slug must be lowercase letters, digits, and hyphens, with no leading or trailing hyphen.",
        )
    existing = db.exec(select(Club).where(Club.slug == slug)).first()
    if existing:
        raise HTTPException(status_code=409, detail="A club with this slug already exists.")

    club = Club(
        name=body.name,
        slug=slug,
        timezone=body.timezone,
        contact_email=body.contact_email,
        active=body.active,
    )
    db.add(club)
    db.flush()
    log_audit(db, user, "club.create", "club", club.id, f"{club.name!r} (slug={club.slug})")
    db.commit()
    db.refresh(club)
    return club


VALID_CADENCES: frozenset[str] = frozenset({"weekly", "fortnightly"})


def _validate_schedule_fields(
    session_day: str, session_cadence: str, cadence_anchor: Optional[date],
    session_start_time: Optional[str] = None,
) -> None:
    """Shared by upsert_club_system (platform-admin, enabling a new
    system for a club) and update_club_system_schedule (club
    self-service, editing an already-enabled system's schedule) — same
    fields, same rules, extracted once rather than duplicated."""
    if session_start_time is not None and not _TIME_RE.match(session_start_time):
        raise HTTPException(status_code=422, detail="session_start_time must match HH:MM (00-23 / 00-59)")

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
    session_start_time: Optional[str] = None


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

    _validate_schedule_fields(body.session_day, body.session_cadence, body.cadence_anchor, body.session_start_time)

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
        session_start_time=body.session_start_time,
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
            "session_start_time": cs.session_start_time,
            # Per-club vibe override (null = falls back to the catalogue
            # default). The catalogue default is surfaced too so the edit
            # form can pre-fill / show what "unset" resolves to.
            "vibe_options": cs.vibe_options,
            "default_vibe": cs.default_vibe,
            "default_vibe_options": sc.vibe_options,
            "default_default_vibe": sc.default_vibe,
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
    user: User = Depends(require_platform_admin),
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
        log_audit(db, user, "club.super_admin.grant", "user", target.id, f"{target.discord_name!r} on club {club.slug!r}")
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
    user: User = Depends(require_platform_admin),
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
        log_audit(db, user, "club.super_admin.revoke", "user", target.id, f"{target.discord_name!r} on club {club.slug!r}")
        db.commit()

    return {"ok": True, "removed": removed}


class ClubActiveBody(BaseModel):
    active: bool


@router.post("/platform/clubs/{club_id}/active")
def set_club_active(
    club_id: int,
    body: ClubActiveBody,
    user: User = Depends(require_platform_admin),
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
        log_audit(db, user, "club.activate" if body.active else "club.deactivate", "club", club.id, club.slug)
        db.commit()
        db.refresh(club)

    return {"id": club.id, "slug": club.slug, "active": club.active}


# Hostname label: lowercase letters/digits/hyphens, no leading/trailing hyphen
# (a club's slug is a subdomain label, <slug>.calltoarms.app).
_SLUG_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


class ClubUpdateBody(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    timezone: Optional[str] = None
    contact_email: Optional[str] = None


@router.patch("/platform/clubs/{club_id}")
def update_club(
    club_id: int,
    body: ClubUpdateBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Partial-update a club's editable details (name, slug, timezone,
    contact email). Platform-admin only. Each field is only touched when
    present in the body — same partial-update pattern as
    PATCH /admin/players/{id}. Active state is handled separately by
    POST .../active and is intentionally not editable here.

    The slug is the club's subdomain identifier (<slug>.calltoarms.app) and
    how the frontend resolves which club a visitor is on, so a slug change
    renames the club's public URL — it's validated for hostname-safe format
    and global uniqueness, same _SLUG_RE check create_club above now uses.
    Only clubs.slug stores this value; every other table references a club
    by club_id, so a slug change is a single-column update with no data
    cascade — the only impact is on external URLs/bookmarks."""
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name cannot be empty.")
        club.name = name

    if body.slug is not None:
        slug = body.slug.strip().lower()
        if not _SLUG_RE.fullmatch(slug):
            raise HTTPException(
                status_code=422,
                detail="Slug must be lowercase letters, digits, and hyphens, with no leading or trailing hyphen.",
            )
        if slug != club.slug:
            clash = db.exec(
                select(Club).where(Club.slug == slug, Club.id != club_id)
            ).first()
            if clash:
                raise HTTPException(status_code=409, detail="A club with this slug already exists.")
            club.slug = slug

    if body.timezone is not None:
        tz = body.timezone.strip()
        if not tz:
            raise HTTPException(status_code=422, detail="Timezone cannot be empty.")
        club.timezone = tz

    if body.contact_email is not None:
        club.contact_email = body.contact_email.strip() or None

    db.add(club)
    log_audit(db, user, "club.update", "club", club.id, f"fields changed: {sorted(body.model_fields_set)}")
    db.commit()
    db.refresh(club)
    return {
        "id": club.id,
        "name": club.name,
        "slug": club.slug,
        "active": club.active,
        "timezone": club.timezone,
        "contact_email": club.contact_email,
    }


# ---------------------------------------------------------------------------
# Per-club-system random mission pool (Call-to-Arms post)
#
# Each club running each system curates its own set of missions — an image
# (uploaded to Supabase Storage via storage.py) plus an optional name and
# optional secondary objectives. The weekly Call-to-Arms post picks one active
# mission at random. Managed by that club's per-system admin, same auth as the
# call-to-arms-settings endpoints (_require_system_scope). Mirrors the
# ClubWebhook per-(club_id, system_id) resource pattern.
# ---------------------------------------------------------------------------

# 5 MB cap — terrain images are ~150 KB today; this is generous headroom while
# still rejecting accidental huge uploads before they hit storage.
MAX_MISSION_IMAGE_BYTES = 5 * 1024 * 1024

MISSION_IMAGE_GUIDELINES = {
    "formats": ["PNG", "JPG", "WEBP"],
    "max_size_mb": 5,
    "recommended": (
        "Use a clear, landscape image of the mission/terrain map. A roughly "
        "16:9 or 4:3 image around 1200px wide looks best in Discord. Avoid "
        "screenshots with heavy UI clutter."
    ),
}


def _mission_row(m: Mission) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "secondary_objectives": m.secondary_objectives,
        "image_url": m.image_url,
        "active": m.active,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _mission_or_404(mission_id: int, user: User, db: Session) -> tuple[Mission, SystemConfig]:
    """Fetch a mission owned by the caller's club and confirm the caller holds
    that system's admin scope. Returns (mission, its SystemConfig)."""
    m = db.get(Mission, mission_id)
    if m is None or m.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Mission not found.")
    config = db.get(SystemConfig, m.system_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Mission's system not found.")
    _require_system_scope(config.legacy_system_name, user, db)
    return m, config


@router.get("/missions")
def list_missions(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """The caller's own club's mission pool for one system, plus the two
    per-club-system toggles and the upload guidelines the UI renders."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    missions = db.exec(
        scoped(Mission, user.club_id)
        .where(Mission.system_id == config.id)
        .order_by(Mission.created_at)
    ).all()
    return {
        "missions_enabled": bool(cs and cs.missions_enabled),
        "missions_use_secondary": bool(cs and cs.missions_use_secondary),
        "guidelines": MISSION_IMAGE_GUIDELINES,
        "missions": [_mission_row(m) for m in missions],
    }


@router.post("/missions", status_code=201)
def create_mission(
    system: str = Form(...),
    name: Optional[str] = Form(None),
    secondary_objectives: Optional[str] = Form(None),
    image: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Upload one mission image + metadata. club_id always comes from the
    session (user.club_id), never the request — same rule as scoped()."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    data = image.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty image upload.")
    if len(data) > MAX_MISSION_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Image too large (max {MAX_MISSION_IMAGE_BYTES // (1024 * 1024)} MB).",
        )
    try:
        storage.extension_for(image.content_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        object_path, public_url = storage.upload_mission_image(
            data, image.content_type, user.club_id, config.id
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {e}")

    m = Mission(
        club_id=user.club_id,
        system_id=config.id,
        name=(name or "").strip() or None,
        secondary_objectives=(secondary_objectives or "").strip() or None,
        image_path=object_path,
        image_url=public_url,
        active=True,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return _mission_row(m)


class MissionPatch(BaseModel):
    name: Optional[str] = None
    secondary_objectives: Optional[str] = None
    active: Optional[bool] = None


@router.patch("/missions/{mission_id}")
def update_mission(
    mission_id: int,
    body: MissionPatch,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Partial update of a mission's metadata (not its image — to change the
    image, delete and re-upload). Only fields present in the body change."""
    m, _config = _mission_or_404(mission_id, user, db)
    provided = body.model_fields_set
    if "name" in provided:
        m.name = (body.name or "").strip() or None
    if "secondary_objectives" in provided:
        m.secondary_objectives = (body.secondary_objectives or "").strip() or None
    if "active" in provided:
        m.active = bool(body.active)
    db.add(m)
    db.commit()
    db.refresh(m)
    return _mission_row(m)


@router.delete("/missions/{mission_id}")
def delete_mission(
    mission_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Delete a mission row and best-effort delete its stored image."""
    m, _config = _mission_or_404(mission_id, user, db)
    object_path = m.image_path
    db.delete(m)
    db.commit()
    storage.delete_mission_image(object_path)
    return {"ok": True}


class MissionsSettingsBody(BaseModel):
    system: str
    missions_enabled: bool
    missions_use_secondary: bool


@router.post("/missions-settings")
def update_missions_settings(
    body: MissionsSettingsBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Set the two per-club-system mission toggles on the caller's ClubSystem
    row."""
    _require_system_scope(body.system, user, db)
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")
    cs.missions_enabled = body.missions_enabled
    cs.missions_use_secondary = body.missions_use_secondary
    db.add(cs)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Club landing page — super-admin manages the club profile, each system's own
# admin manages that system's carousel card + events. Same ownership split as
# the rest of this file (missions/webhooks are per-system; roles/blocks are
# super-admin-only).
# ---------------------------------------------------------------------------

MAX_CLUB_IMAGE_BYTES = 5 * 1024 * 1024


class ClubProfileBody(BaseModel):
    blurb: Optional[str] = None
    website_url: Optional[str] = None
    discord_url: Optional[str] = None
    opening_hours: Optional[list[dict]] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


def _club_profile_dict(club: Club) -> dict:
    return {
        "blurb": club.blurb,
        "logo_url": club.logo_url,
        "website_url": club.website_url,
        "discord_url": club.discord_url,
        "opening_hours": club.opening_hours or [],
        "address": club.address,
        "latitude": club.latitude,
        "longitude": club.longitude,
    }


def _apply_club_profile_update(club: Club, body: ClubProfileBody, db: Session) -> Club:
    """Shared by the club's own super-admin (POST /admin/club, always their
    own club) and a platform admin editing any club
    (POST /admin/platform/clubs/{club_id}/profile) — same fields, same
    validation, extracted once rather than duplicated."""
    if body.opening_hours is not None:
        for row in body.opening_hours:
            if row.get("day") not in _DAY_NAME_TO_INT:
                raise HTTPException(status_code=422, detail=f"Invalid day: {row.get('day')!r}")

    if body.latitude is not None and not (-90 <= body.latitude <= 90):
        raise HTTPException(status_code=422, detail="latitude must be between -90 and 90.")
    if body.longitude is not None and not (-180 <= body.longitude <= 180):
        raise HTTPException(status_code=422, detail="longitude must be between -180 and 180.")

    provided = body.model_fields_set
    if "blurb" in provided:
        club.blurb = (body.blurb or "").strip() or None
    if "website_url" in provided:
        club.website_url = (body.website_url or "").strip() or None
    if "discord_url" in provided:
        club.discord_url = (body.discord_url or "").strip() or None
    if "opening_hours" in provided:
        club.opening_hours = body.opening_hours
    if "address" in provided:
        club.address = (body.address or "").strip() or None
    if "latitude" in provided:
        club.latitude = body.latitude
    if "longitude" in provided:
        club.longitude = body.longitude
    db.add(club)
    db.commit()
    db.refresh(club)
    return club


def _upload_club_logo_for(club: Club, image: UploadFile, db: Session) -> str:
    """Shared by the club's own super-admin and a platform admin uploading
    on any club's behalf."""
    data = image.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty image upload.")
    if len(data) > MAX_CLUB_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Image too large (max {MAX_CLUB_IMAGE_BYTES // (1024 * 1024)} MB).",
        )
    try:
        storage.extension_for(image.content_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        object_path, public_url = storage.upload_club_logo(data, image.content_type, club.id)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {e}")

    old_path = club.logo_path
    club.logo_path = object_path
    club.logo_url = public_url
    db.add(club)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)
    return public_url


def _delete_club_logo_for(club: Club, db: Session) -> None:
    old_path = club.logo_path
    club.logo_path = None
    club.logo_url = None
    db.add(club)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)


@router.post("/club")
def update_club_profile(
    body: ClubProfileBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Super-admin edits the caller's own club's landing-page profile.
    club_id always comes from the session (user.club_id)."""
    club = db.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")
    club = _apply_club_profile_update(club, body, db)
    return _club_profile_dict(club)


@router.post("/club/logo", status_code=201)
def upload_club_logo(
    image: UploadFile = File(...),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")
    public_url = _upload_club_logo_for(club, image, db)
    return {"logo_url": public_url}


@router.delete("/club/logo")
def delete_club_logo(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")
    _delete_club_logo_for(club, db)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Platform admin — editing any club's landing-page profile (location,
# opening hours, logo). Same fields/validation as the club's own super-admin
# self-service above, just addressed by club_id instead of user.club_id.
# ---------------------------------------------------------------------------

@router.get("/platform/clubs/{club_id}/profile")
def get_platform_club_profile(
    club_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")
    return _club_profile_dict(club)


@router.post("/platform/clubs/{club_id}/profile")
def update_platform_club_profile(
    club_id: int,
    body: ClubProfileBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")
    club = _apply_club_profile_update(club, body, db)
    return _club_profile_dict(club)


@router.post("/platform/clubs/{club_id}/logo", status_code=201)
def upload_platform_club_logo(
    club_id: int,
    image: UploadFile = File(...),
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")
    public_url = _upload_club_logo_for(club, image, db)
    return {"logo_url": public_url}


@router.delete("/platform/clubs/{club_id}/logo")
def delete_platform_club_logo(
    club_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")
    _delete_club_logo_for(club, db)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Platform admin tools: site banner, scheduled-job health check, audit log,
# cross-club user lookup.
# ---------------------------------------------------------------------------

class PlatformBannerBody(BaseModel):
    message: str
    severity: str = "info"
    active: bool = False


_VALID_BANNER_SEVERITIES = frozenset({"info", "warning", "critical"})


def _banner_dict(b: PlatformBanner) -> dict:
    return {
        "message": b.message,
        "severity": b.severity,
        "active": b.active,
        "updated_at": b.updated_at,
    }


@router.get("/platform/banner")
def get_platform_banner_admin(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Platform-admin view of the banner (unlike GET /platform-banner in
    main.py, which is public and only ever returns it when active)."""
    banner = db.get(PlatformBanner, 1)
    if banner is None:
        return {"message": "", "severity": "info", "active": False, "updated_at": None}
    return _banner_dict(banner)


@router.post("/platform/banner")
def set_platform_banner(
    body: PlatformBannerBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    if body.severity not in _VALID_BANNER_SEVERITIES:
        raise HTTPException(
            status_code=422, detail=f"severity must be one of: {sorted(_VALID_BANNER_SEVERITIES)}"
        )
    banner = db.get(PlatformBanner, 1)
    if banner is None:
        banner = PlatformBanner(id=1, message=body.message, severity=body.severity, active=body.active)
    else:
        banner.message = body.message
        banner.severity = body.severity
        banner.active = body.active
        banner.updated_at = datetime.utcnow()
    db.add(banner)
    log_audit(
        db, user, "banner.update", "platform_banner", 1,
        f"active={body.active} severity={body.severity!r}",
    )
    db.commit()
    db.refresh(banner)
    return _banner_dict(banner)


# Every scheduled job that calls record_job_run() — kept as one list so the
# health-check UI can show "never run" for a job with zero rows, not just
# silently omit it.
KNOWN_SCHEDULED_JOBS = ["auto_pairings_check", "call_to_arms_check"]


def _job_run_dict(r: ScheduledJobRun) -> dict:
    return {"job_name": r.job_name, "ran_at": r.ran_at, "status": r.status, "detail": r.detail}


@router.get("/platform/job-runs")
def list_job_runs(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Health-check view of the two scheduled GitHub Actions jobs — the
    most recent run of each (so a silently-broken cron is visible instead
    of only surfacing when a club complains their pairings never posted),
    plus a short recent history across both for spotting flakiness."""
    jobs = []
    for job_name in KNOWN_SCHEDULED_JOBS:
        latest = db.exec(
            select(ScheduledJobRun)
            .where(ScheduledJobRun.job_name == job_name)
            .order_by(ScheduledJobRun.ran_at.desc())
        ).first()
        jobs.append({"job_name": job_name, "latest": _job_run_dict(latest) if latest else None})

    recent = db.exec(
        select(ScheduledJobRun).order_by(ScheduledJobRun.ran_at.desc()).limit(20)
    ).all()
    return {"jobs": jobs, "recent": [_job_run_dict(r) for r in recent]}


@router.get("/platform/audit-log")
def list_audit_log(
    limit: int = 100,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    limit = max(1, min(limit, 500))
    rows = db.exec(
        select(AuditLogEntry).order_by(AuditLogEntry.created_at.desc()).limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at,
            "actor_name": r.actor_name,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "detail": r.detail,
        }
        for r in rows
    ]


@router.get("/platform/users/search")
def search_users(
    q: str,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Cross-club lookup by Discord name or linked player name — for
    support ("my account's broken") when you don't know which club the
    reporter belongs to, unlike every other platform-admin user-facing
    endpoint which is scoped to one club_id at a time."""
    query = q.strip()
    if not query:
        return []
    like = f"%{query}%"
    rows = db.exec(
        select(User, Club)
        .join(Club, Club.id == User.club_id)
        .where(User.discord_name.ilike(like))
        .order_by(User.discord_name)
        .limit(50)
    ).all()
    matched_ids = {u.id for u, _ in rows}

    player_rows = db.exec(
        select(User, Club, Player)
        .join(Club, Club.id == User.club_id)
        .join(Player, Player.id == User.player_id)
        .where(Player.name.ilike(like))
        .order_by(Player.name)
        .limit(50)
    ).all()

    result = []
    for u, c in rows:
        p = db.get(Player, u.player_id) if u.player_id else None
        result.append({
            "user_id": u.id, "discord_name": u.discord_name,
            "player_name": p.name if p else None,
            "club_id": c.id, "club_name": c.name, "club_slug": c.slug,
            "is_super_admin": u.is_super_admin, "is_platform_admin": u.is_platform_admin,
        })
    for u, c, p in player_rows:
        if u.id in matched_ids:
            continue
        matched_ids.add(u.id)
        result.append({
            "user_id": u.id, "discord_name": u.discord_name,
            "player_name": p.name,
            "club_id": c.id, "club_name": c.name, "club_slug": c.slug,
            "is_super_admin": u.is_super_admin, "is_platform_admin": u.is_platform_admin,
        })
    return result[:50]


def _club_request_dict(r: ClubRequest) -> dict:
    return {
        "id": r.id,
        "created_at": r.created_at,
        "status": r.status,
        "requester_name": r.requester_name,
        "requester_email": r.requester_email,
        "club_name": r.club_name,
        "club_location": r.club_location,
        "notes": r.notes,
        "reviewed_at": r.reviewed_at,
        "reviewed_by_name": r.reviewed_by_name,
    }


@router.get("/platform/club-requests")
def list_club_requests(
    status: Optional[str] = None,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """"Please add my club" submissions from the logged-out hero page.
    ?status=pending|approved|denied filters; omitted returns all, newest
    first."""
    query = select(ClubRequest)
    if status is not None:
        if status not in {"pending", "approved", "denied"}:
            raise HTTPException(status_code=422, detail="status must be one of: pending, approved, denied")
        query = query.where(ClubRequest.status == status)
    rows = db.exec(query.order_by(ClubRequest.created_at.desc())).all()
    return [_club_request_dict(r) for r in rows]


def _review_club_request(request_id: int, new_status: str, user: User, db: Session) -> ClubRequest:
    req = db.get(ClubRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Club request not found.")
    req.status = new_status
    req.reviewed_at = datetime.utcnow()
    req.reviewed_by_user_id = user.id
    req.reviewed_by_name = user.discord_name
    db.add(req)
    log_audit(
        db, user, f"club_request.{new_status}", "club_request", req.id,
        f"{req.club_name!r} ({req.requester_email})",
    )
    db.commit()
    db.refresh(req)
    return req


@router.post("/platform/club-requests/{request_id}/approve")
def approve_club_request(
    request_id: int,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Marks the request reviewed/approved — does not create the Club row
    itself. The real club is still created by hand via the existing
    POST /admin/platform/clubs flow, since Joel emails the requester a
    getting-started pack as part of onboarding them."""
    return _club_request_dict(_review_club_request(request_id, "approved", user, db))


@router.post("/platform/club-requests/{request_id}/deny")
def deny_club_request(
    request_id: int,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    return _club_request_dict(_review_club_request(request_id, "denied", user, db))


class ClubSystemCarouselBody(BaseModel):
    system: str
    blurb: Optional[str] = None
    accent_color: Optional[str] = None


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@router.post("/club-systems/carousel")
def update_carousel_card(
    body: ClubSystemCarouselBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """That system's own admin edits its Club-page carousel card (blurb,
    accent colour). Photo is a separate upload endpoint below. Ordering is
    deliberately not admin-settable — GET /club always returns systems in a
    fixed order and the frontend shuffles them itself on every page load,
    so no system can be permanently favoured by ordering."""
    _require_system_scope(body.system, user, db)
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")

    if body.accent_color is not None and body.accent_color != "" and not _HEX_COLOR_RE.match(body.accent_color):
        raise HTTPException(status_code=422, detail="accent_color must be a hex colour like #c9a14a.")

    provided = body.model_fields_set
    if "blurb" in provided:
        cs.carousel_blurb = (body.blurb or "").strip() or None
    if "accent_color" in provided:
        cs.accent_color = body.accent_color or None
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return {
        "blurb": cs.carousel_blurb,
        "photo_url": cs.carousel_photo_url,
        "accent_color": cs.accent_color,
    }


@router.post("/club-systems/carousel/photo", status_code=201)
def upload_carousel_photo(
    system: str = Form(...),
    image: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")

    data = image.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty image upload.")
    if len(data) > MAX_CLUB_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Image too large (max {MAX_CLUB_IMAGE_BYTES // (1024 * 1024)} MB).",
        )
    try:
        storage.extension_for(image.content_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        object_path, public_url = storage.upload_carousel_photo(
            data, image.content_type, user.club_id, config.id
        )
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {e}")

    old_path = cs.carousel_photo_path
    cs.carousel_photo_path = object_path
    cs.carousel_photo_url = public_url
    db.add(cs)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)
    return {"photo_url": public_url}


@router.delete("/club-systems/carousel/photo")
def delete_carousel_photo(
    system: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")
    old_path = cs.carousel_photo_path
    cs.carousel_photo_path = None
    cs.carousel_photo_url = None
    db.add(cs)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)
    return {"ok": True}


def _event_row(ev: ClubEvent) -> dict:
    return {
        "id": ev.id,
        "system_id": ev.system_id,
        "title": ev.title,
        "description": ev.description,
        "event_date": ev.event_date.isoformat(),
        "start_time": ev.start_time,
        "end_time": ev.end_time,
        "all_day": ev.all_day,
    }


@router.get("/club/events")
def list_club_events(
    system: Optional[str] = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """List this club's events. ?system=<legacy name> filters to one
    system's own events; omitted returns everything the caller can manage
    (super-admin: all; system admin: only their own systems' events)."""
    query = scoped(ClubEvent, user.club_id)
    if system is not None:
        config = _get_system_config(db, system)
        if config is None:
            raise HTTPException(status_code=422, detail="Unknown system.")
        _require_system_scope(system, user, db)
        query = query.where(ClubEvent.system_id == config.id)
    elif not user.is_super_admin:
        owned_ids = {
            c.id for c in db.exec(
                select(SystemConfig).where(SystemConfig.legacy_system_name.in_(admin_scopes(user, db)))
            ).all()
        }
        if not owned_ids:
            return []
        query = query.where(ClubEvent.system_id.in_(owned_ids))
    rows = db.exec(query.order_by(ClubEvent.event_date)).all()
    return [_event_row(r) for r in rows]


class ClubEventBody(BaseModel):
    system: Optional[str] = None  # None = club-wide event, super-admin only
    title: str
    description: Optional[str] = None
    event_date: date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    all_day: bool = False


def _resolve_event_system_id(body_system: Optional[str], user: User, db: Session) -> Optional[int]:
    if body_system is None:
        if not user.is_super_admin:
            raise HTTPException(status_code=403, detail="Only a super-admin can create a club-wide event.")
        return None
    _require_system_scope(body_system, user, db)
    config = _get_system_config(db, body_system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    return config.id


@router.post("/club/events", status_code=201)
def create_club_event(
    body: ClubEventBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    system_id = _resolve_event_system_id(body.system, user, db)
    ev = ClubEvent(
        club_id=user.club_id,
        system_id=system_id,
        title=body.title.strip(),
        description=(body.description or "").strip() or None,
        event_date=body.event_date,
        start_time=body.start_time,
        end_time=body.end_time,
        all_day=body.all_day,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return _event_row(ev)


def _event_or_404(event_id: int, user: User, db: Session) -> ClubEvent:
    ev = db.get(ClubEvent, event_id)
    if ev is None or ev.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Event not found")
    if ev.system_id is None:
        if not user.is_super_admin:
            raise HTTPException(status_code=403, detail="Only a super-admin can manage a club-wide event.")
    else:
        config = db.get(SystemConfig, ev.system_id)
        if config is None or config.legacy_system_name not in admin_scopes(user, db):
            raise HTTPException(status_code=403, detail="Admin access for this system required.")
    return ev


class ClubEventPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    event_date: Optional[date] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    all_day: Optional[bool] = None


@router.patch("/club/events/{event_id}")
def update_club_event(
    event_id: int,
    body: ClubEventPatch,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    ev = _event_or_404(event_id, user, db)
    provided = body.model_fields_set
    if "title" in provided and body.title is not None:
        ev.title = body.title.strip()
    if "description" in provided:
        ev.description = (body.description or "").strip() or None
    if "event_date" in provided and body.event_date is not None:
        ev.event_date = body.event_date
    if "start_time" in provided:
        ev.start_time = body.start_time
    if "end_time" in provided:
        ev.end_time = body.end_time
    if "all_day" in provided and body.all_day is not None:
        ev.all_day = body.all_day
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return _event_row(ev)


@router.delete("/club/events/{event_id}")
def delete_club_event(
    event_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    ev = _event_or_404(event_id, user, db)
    db.delete(ev)
    db.commit()
    return {"ok": True}
