"""Admin endpoints for a week's pairings: generate, edit, publish and post them,
the blocks that keep two players apart, game history, and the per-system
pairing weights. Split out of admin.py; shared helpers still live there.
"""
from urllib.parse import quote
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    admin_scopes,
    get_session,
    require_admin,
    valid_scopes,
)
from database import scoped, posting_enabled, posting_key, POSTING_KINDS, system_setting_slug as _slug, get_setting as _get_setting, upsert_setting as _upsert_setting, log_audit, resolve_webhook_url, club_app_url
from models import Club, ClubSetting, ClubSystem, Mission, PairingBlock, Pairing, PairingConfig, Player, PublishState, Signup, SystemConfig, User
from observability import capture
from levels import announce_level_ups
import call_to_arms_content as cta_content
from pairings_engine import generate, _get_pairing_config, summarize_pairings
from system_overrides import effective_system
import week_pairings
from systems import (
    resolved_faction_groups,
)
from table_booking import maybe_send_table_booking
from signups import (
    _get_system_config,
    _require_system_enabled,
    signup_cap,
)
from github_dispatch import PAIRINGS_IMAGE_WORKFLOW, dispatch_workflow
from week_logic import next_session_date

from admin import (
    _require_system_scope,
    _resync_venue_floor,
    _system_id_for,
    _TIME_RE,
    LR_DEFAULT_DAY,
    LR_DEFAULT_TIME,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_require_any_admin = require_admin


# ---------------------------------------------------------------------------
# Pairing blocks (canonical low < high storage; system_id NULL = club-wide)
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
    system: Optional[str] = None,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Pairing blocks, enriched with player names, sorted A→B.

    No `system`: every block the club has, which is what the club-level
    Players & blocks tab shows.

    With `system`: the blocks that would actually apply when pairing it, which
    is both its own and the club-wide ones. The club-wide rows are marked so
    the system tab can show them without implying its admin may remove them.
    """
    block_rows = db.exec(scoped(PairingBlock, user.club_id)).all()

    system_id = None
    if system:
        _require_system_scope(system, user, db)
        system_id = _system_id_for(db, system)
        block_rows = [
            b for b in block_rows if b.system_id is None or b.system_id == system_id
        ]

    player_ids = {b.player_a_id for b in block_rows} | {b.player_b_id for b in block_rows}
    players_by_id: dict[int, Player] = {}
    if player_ids:
        rows = db.exec(scoped(Player, user.club_id).where(Player.id.in_(player_ids))).all()
        players_by_id = {p.id: p for p in rows}

    def _name(pid: int) -> str:
        p = players_by_id.get(pid)
        return p.name if p else f"#{pid}"

    # System names for the club-level view, which lists blocks from every
    # system at once and has to say which is which. One query, not one per row.
    system_names: dict[int, str] = {}
    scoped_ids = {b.system_id for b in block_rows if b.system_id is not None}
    if scoped_ids:
        system_names = {
            sc.id: sc.legacy_system_name
            for sc in db.exec(select(SystemConfig).where(SystemConfig.id.in_(scoped_ids))).all()
        }

    result = [
        {
            "block_id": b.id,
            "player_a_id": b.player_a_id,
            "player_a_name": _name(b.player_a_id),
            "player_b_id": b.player_b_id,
            "player_b_name": _name(b.player_b_id),
            "note": b.note,
            "system": system_names.get(b.system_id) if b.system_id else None,
            "club_wide": b.system_id is None,
        }
        for b in block_rows
    ]
    result.sort(key=lambda x: (x["player_a_name"].lower(), x["player_b_name"].lower()))
    return result


class BlockBody(BaseModel):
    player_a_id: int
    player_b_id: int
    note: Optional[str] = None
    # Omit for a club-wide block (super-admin only). Name it to block the pair
    # in that system alone, which its own admin may do.
    system: Optional[str] = None


def _authorize_block(system: Optional[str], user: User, db: Session) -> Optional[int]:
    """Check the caller may write this block, and return its system_id.

    Two different powers behind one endpoint. A club-wide block reaches every
    game night the club runs, so it stays super-admin only — exactly who could
    create one before this existed. A system-scoped block only affects the
    system named, so that system's own admin may create it, the same way they
    already own its pairings and its schedule.

    A scope admin therefore cannot widen a block to the whole club by leaving
    the field off, which is the escalation worth being explicit about.
    """
    if not system:
        if not (user.is_platform_admin or user.is_super_admin):
            raise HTTPException(
                status_code=403,
                detail="A club-wide block is a super-admin's call. Name a system to block a pair for that system only.",
            )
        return None
    _require_system_scope(system, user, db)
    return _system_id_for(db, system)


@router.post("/blocks")
def add_block(
    body: BlockBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Insert a canonical (low, high) block. Idempotent; updates note if block exists."""
    if body.player_a_id == body.player_b_id:
        raise HTTPException(status_code=422, detail="Cannot block a player from themselves.")

    system_id = _authorize_block(body.system, user, db)

    low, high = sorted([body.player_a_id, body.player_b_id])
    note = (body.note or "").strip() or None

    # Uniqueness is per (club, pair, system), not per (club, pair). The same
    # two players can carry a club-wide block AND a system one, and can be
    # blocked in two systems independently — so a lookup that ignored
    # system_id would silently edit the wrong row's note, or refuse to create
    # the second block at all.
    existing = db.exec(
        scoped(PairingBlock, user.club_id)
        .where(PairingBlock.player_a_id == low)
        .where(PairingBlock.player_b_id == high)
        .where(PairingBlock.system_id == system_id)
    ).first()

    if existing is not None:
        if note:
            existing.note = note
            db.add(existing)
            db.commit()
        return {"ok": True, "created": False}

    db.add(PairingBlock(
        player_a_id=low, player_b_id=high, note=note,
        club_id=user.club_id, system_id=system_id,
    ))
    db.commit()
    return {"ok": True, "created": True}


@router.delete("/blocks")
def remove_block(
    player_a_id: int,
    player_b_id: int,
    system: Optional[str] = None,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Delete the canonical (low, high) block if it exists.

    Same authorization as creating one: a scope admin can lift their own
    system's block and cannot touch a club-wide one.
    """
    system_id = _authorize_block(system, user, db)
    low, high = sorted([player_a_id, player_b_id])
    row = db.exec(
        scoped(PairingBlock, user.club_id)
        .where(PairingBlock.player_a_id == low)
        .where(PairingBlock.player_b_id == high)
        .where(PairingBlock.system_id == system_id)
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
    user: User = Depends(_require_any_admin),
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
    # "Open" was "Either" before 2026-08-28 and old signups still say so, so a
    # pairing from an archived week resolves the same way it always did.
    a_open = av_l in ("open", "either")
    b_open = bv_l in ("open", "either")
    if a_open and b_open:
        return "Open"
    if a_open and bv:
        return bv
    if b_open and av:
        return av
    return av or bv or None


def _eta_show(a_su: Optional[Signup], b_su: Optional[Signup],
              override: Optional[str] = None) -> Optional[str]:
    """The game's time: what an admin set on the pairing, else the later of the
    two signup times (both players can make that). See Pairing's note: the
    signups say what each player asked for and are never rewritten from here."""
    if override:
        return override
    a_eta = a_su.eta if a_su else None
    b_eta = b_su.eta if b_su else None
    if a_eta and b_eta:
        return max(a_eta, b_eta)
    return a_eta or b_eta


def _pts_show(a_su: Optional[Signup], b_su: Optional[Signup], uses_points: bool,
              override: Optional[int] = None) -> Optional[str]:
    if not uses_points:
        return None
    if isinstance(override, int):
        return str(override)
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
    player_levels: Optional[dict] = None,
    eta_override: Optional[str] = None,
    points_override: Optional[int] = None,
    a_vibe_override: Optional[str] = None,
    b_vibe_override: Optional[str] = None,
) -> dict:
    a_su = signups_by_id.get(a_signup_id)
    b_su = signups_by_id.get(b_signup_id) if b_signup_id else None

    a_name = a_su.player_name if a_su else f"#{a_signup_id}"
    a_vibe = a_vibe_override or (a_su.vibe if a_su else None)

    b_name = (b_su.player_name if b_su else f"#{b_signup_id}") if b_signup_id else "BYE"
    b_vibe = b_vibe_override or (b_su.vibe if b_su else None)

    return {
        "id": row_id,
        "a_signup_id": a_signup_id,
        "a_name": a_name,
        # "" on the pairing is an admin clearing it; NULL means follow the signup.
        "a_faction": (a_faction or None) if a_faction is not None else (a_su.faction if a_su else None),
        "a_vibe": a_vibe,
        # Self-reported experience ("New" / "Some" / "Veteran"). Carried
        # through to the posted pairings image so the level each player put on
        # their own signup is visible to their opponent before the game, not
        # just to the matcher that used it to pair them.
        "a_experience": a_su.experience if a_su else None,
        # Level for the posted image. None when not supplied, so the admin
        # grid — which doesn't need it — pays for nothing.
        "a_level": (player_levels or {}).get(a_su.player_id) if a_su else None,
        "b_signup_id": b_signup_id,
        "b_name": b_name,
        "b_faction": (((b_faction or None) if b_faction is not None else (b_su.faction if b_su else None))
                      if b_signup_id else None),
        "b_vibe": b_vibe,
        "b_experience": b_su.experience if b_su else None,
        "b_level": (player_levels or {}).get(b_su.player_id) if b_su else None,
        "type": _public_vibe_display(a_vibe, b_vibe),
        "eta": _eta_show(a_su, b_su, eta_override),
        "points": _pts_show(a_su, b_su, uses_points, points_override),
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


def _pairing_rows_to_display(pairings: list, signups_by_id: dict, uses_points: bool,
                             player_levels: Optional[dict] = None) -> list:
    result = []
    for p in pairings:
        # Pass the pairing's own value through untouched, including "" for a
        # faction an admin cleared: _build_display_row decides what NULL and ""
        # mean. Collapsing "" here showed the signup's faction back to the
        # admin while the public card correctly showed none.
        a_faction = p.a_faction
        b_faction = p.b_faction if p.b_signup_id else None
        result.append(_build_display_row(
            p.id, p.a_signup_id, p.b_signup_id,
            a_faction, b_faction,
            p.prearranged, signups_by_id, uses_points, player_levels,
            eta_override=p.eta, points_override=p.points,
            a_vibe_override=p.a_vibe, b_vibe_override=p.b_vibe,
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




class AutoPairingsSettingsBody(BaseModel):
    system: str
    enabled: bool
    day: str
    time: str


@router.get("/auto-pairings-settings")
def get_auto_pairings_settings(
    system: str,
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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


class LeagueRankingsSettingsBody(BaseModel):
    system: str
    day: str
    time: str


@router.get("/league-rankings-settings")
def get_league_rankings_settings(
    system: str,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """When this club-system's league standings image gets posted.

    There is no `enabled` here on purpose. Whether standings post at all is the
    `league` posting switch on the Discord tab, which also covers results and
    achievements — one decision, one switch. This returns its current value so
    the League panel can say what is going to happen without offering a second
    control that could disagree with the first.
    """
    _require_system_scope(system, user, db)
    slug = _slug(system)
    config = _get_system_config(db, system)
    return {
        "day": _get_setting(db, user.club_id, f"league_rankings_{slug}_day", LR_DEFAULT_DAY) or LR_DEFAULT_DAY,
        "time": _get_setting(db, user.club_id, f"league_rankings_{slug}_time", LR_DEFAULT_TIME) or LR_DEFAULT_TIME,
        "last_posted": _get_setting(db, user.club_id, f"league_rankings_{slug}_last_posted"),
        # Read-only context, so the panel can explain itself rather than
        # silently doing nothing when one of these is missing.
        "posting_enabled": posting_enabled(db, user.club_id, system, "league"),
        "has_webhook": bool(
            resolve_webhook_url(db, user.club_id, "league_rankings", config.id if config else None)
        ),
    }


@router.post("/league-rankings-settings")
def post_league_rankings_settings(
    body: LeagueRankingsSettingsBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    _require_system_scope(body.system, user, db)
    if body.day not in _VALID_DAYS:
        raise HTTPException(status_code=422, detail=f"day must be one of {_VALID_DAYS}")
    if not _TIME_RE.match(body.time):
        raise HTTPException(status_code=422, detail="time must match HH:MM (00-23 / 00-59)")
    slug = _slug(body.system)
    _upsert_setting(db, user.club_id, f"league_rankings_{slug}_day", body.day)
    _upsert_setting(db, user.club_id, f"league_rankings_{slug}_time", body.time)
    # Unlike the auto-pairings settings, this one is audited. Being unable to
    # answer "who changed this, and when" is what made an earlier scheduling
    # incident take far longer to diagnose than it should have.
    log_audit(db, user, "league_rankings_schedule.update", "club_system", None,
              f"system={body.system} day={body.day} time={body.time}")
    db.commit()
    return {"ok": True}


class SignupCapSettingsBody(BaseModel):
    system: str
    enabled: bool
    tables: int


@router.get("/signup-cap-settings")
def get_signup_cap_settings(
    system: str,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Per-(club, system) signup cap: enable toggle + table limit. The player
    limit is tables × players-per-table (reused from table-booking config)."""
    _require_system_scope(system, user, db)
    cap = signup_cap(db, user.club_id, system)
    return {
        "enabled": cap["enabled"],
        "tables": cap["tables"],
        "players_per_table": cap["players_per_table"],
        "max_players": cap["tables"] * cap["players_per_table"] if cap["tables"] > 0 else 0,
    }


@router.post("/signup-cap-settings")
def post_signup_cap_settings(
    body: SignupCapSettingsBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    _require_system_scope(body.system, user, db)
    if body.enabled and body.tables < 1:
        raise HTTPException(status_code=422, detail="Set at least 1 table when the cap is enabled.")
    slug = _slug(body.system)
    _upsert_setting(db, user.club_id, f"signup_cap_{slug}_enabled", "true" if body.enabled else "false")
    _upsert_setting(db, user.club_id, f"signup_cap_{slug}_tables", str(max(0, body.tables)))
    db.commit()
    cap = signup_cap(db, user.club_id, body.system)
    return {
        "enabled": cap["enabled"],
        "tables": cap["tables"],
        "players_per_table": cap["players_per_table"],
        "max_players": cap["tables"] * cap["players_per_table"] if cap["tables"] > 0 else 0,
    }


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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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


class PostSwitchBody(BaseModel):
    system: str
    kind: str
    enabled: bool


@router.get("/post-switches")
def get_post_switches(
    system: str,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Which Discord posts are switched on for this system. All default on."""
    _require_system_scope(system, user, db)
    return {k: posting_enabled(db, user.club_id, system, k) for k in POSTING_KINDS}


@router.post("/post-switches")
def set_post_switch(
    body: PostSwitchBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Turn one kind of Discord post on or off for one system.

    Separate from the webhook itself on purpose: turning posts off keeps the
    URL, so a club going live does not have to go and find it again.
    """
    _require_system_scope(body.system, user, db)
    if body.kind not in POSTING_KINDS:
        raise HTTPException(status_code=422, detail=f"Unknown post kind: {body.kind}")
    _upsert_setting(db, user.club_id, posting_key(body.kind, body.system),
                    "true" if body.enabled else "false")
    db.commit()
    return {"ok": True, body.kind: body.enabled}


class CallToArmsPostNowBody(BaseModel):
    system: str


@router.post("/call-to-arms-post-now")
def call_to_arms_post_now(
    body: CallToArmsPostNowBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Post this system's call-to-arms message now, whatever the schedule says.

    A temporary override and a test button: for checking a template change
    renders the way you meant, or shouting about a session the schedule is
    about to be too late for.

    WRITES NOTHING. It reads `enabled`, `days_before`, `time` and `last_week`
    not at all, and changes none of them, so the schedule is bit-for-bit as it
    was and keeps running exactly as before — including for this session. If
    that means the same call goes out twice this week, that's the intended
    behaviour of an override, not an accident: whoever pressed the button knew
    the scheduled one was still coming.

    Everything about the message itself (template, image, mission pool, session
    date) is resolved exactly as scripts/run_call_to_arms_check.py resolves it,
    so what goes out is what would have gone out.
    """
    _require_system_scope(body.system, user, db)
    _require_system_enabled(db, user.club_id, body.system)

    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=404, detail="Unknown system.")

    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None or not cs.session_day:
        raise HTTPException(
            status_code=422,
            detail="This system has no session day set, so there's no session to call people to.",
        )

    webhook_url = resolve_webhook_url(db, user.club_id, "call_to_arms", config.id)
    if not webhook_url:
        raise HTTPException(
            status_code=422,
            detail="No call-to-arms webhook set for this system. Add one in the Discord panel first.",
        )

    # UK time, exactly as scripts/run_call_to_arms_check.py works it out. The
    # manual post and the scheduled one must agree on which session is next, or
    # posting by hand near midnight could target a different week to the cron.
    today = datetime.now(ZoneInfo("Europe/London")).date()
    try:
        next_session = next_session_date(
            cs.session_day, cs.session_cadence, cs.cadence_anchor, today,
        )
    except AssertionError:
        raise HTTPException(
            status_code=422,
            detail="This system runs fortnightly or monthly but has no anchor date, "
                   "so the next session can't be worked out.",
        )

    slug = _slug(body.system)
    template = (
        _get_setting(db, user.club_id, f"call_to_arms_{slug}_template")
        or cta_content.default_template(body.system)
    )
    image_mode, image_url = cta_content.parse_image_setting(
        _get_setting(db, user.club_id, f"call_to_arms_{slug}_image")
    )
    missions = None
    if cs.missions_enabled:
        missions = [
            {"name": m.name, "secondary_objectives": m.secondary_objectives,
             "image_url": m.image_url}
            for m in db.exec(
                select(Mission)
                .where(Mission.club_id == user.club_id)
                .where(Mission.system_id == config.id)
                .where(Mission.active == True)
            ).all()
        ]

    # Same link the scheduled post builds — the club's own subdomain and this
    # system's signup tab. It used to pass APP_PUBLIC_URL, which is only ever
    # set on the GitHub Actions runner, so in the API process it was the empty
    # string and every manual post rendered {signup_url} as nothing at all.
    club = db.get(Club, user.club_id)
    signup_url = club_app_url(club, f"/signup?system={quote(body.system)}")

    try:
        cta_content.post(
            webhook_url, template, body.system, next_session, signup_url,
            image_mode=image_mode, image_url=image_url, missions=missions,
        )
    except Exception as e:
        capture(e, kind="call_to_arms_manual_post", system=body.system)
        raise HTTPException(
            status_code=502,
            detail="Discord wouldn't take the message. Check the webhook is still valid.",
        )

    # Deliberately no _upsert_setting here: see the docstring. The schedule is
    # untouched, so the automatic post still goes out on its own day.
    return {"ok": True, "session_date": next_session.strftime("%d/%m/%Y")}


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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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

    config = effective_system(db, user.club_id, _get_system_config(db, body.system))
    uses_points = config.uses_points if config else (body.system != "Kill Team")
    display = _pairing_rows_to_display(prearranged, signups_by_id, uses_points) + \
              _dicts_to_display(proposed_dicts, signups_by_id, uses_points)

    summary = summarize_pairings(db, body.system, body.week, user.club_id, _pairs_of(all_rows))
    return {"rows": display, "preview": True, "summary": summary}




@router.post("/pairings/generate")
def pairings_generate(
    body: PairingsWeekBody,
    user: User = Depends(_require_any_admin),
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

    _resync_venue_floor(db, user.club_id, body.system, body.week)

    signups_by_id = _collect_signups_for_rows(new_pairings, db, user.club_id)
    config = effective_system(db, user.club_id, _get_system_config(db, body.system))
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
    user: User = Depends(_require_any_admin),
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
    config = effective_system(db, user.club_id, _get_system_config(db, system))
    uses_points = config.uses_points if config else (system != "Kill Team")
    display = _pairing_rows_to_display(rows, signups_by_id, uses_points)
    summary = summarize_pairings(db, system, week, user.club_id, _pairs_of(rows)) if rows else None
    return {"rows": display, "published": published, "summary": summary}


@router.post("/pairings/publish")
def pairings_publish(
    body: PublishBody,
    user: User = Depends(_require_any_admin),
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
        # The pairings ARE the answer to "how many tables does tonight need",
        # so this is the moment the venue's floor can be laid out — without
        # anyone on the venue side having to know pairings happened. Swallows
        # its own errors: a floor plan must never be able to block a publish.
        try:
            from venue_seating import lay_out_on_publish

            # create=True: publishing is the one moment allowed to lay a floor
            # out from nothing. Every later change only keeps it in step.
            lay_out_on_publish(db, user.club_id, body.system, body.week)
        except Exception as e:
            capture(e, kind="venue_seating_on_publish", system=body.system)
        # Levels are derived from pairings, so publishing is the moment a
        # week's games become official and someone's bar moves. Announcing
        # here means the "ding" lands alongside the pairings post rather than
        # at some arbitrary hour. Never raises — a webhook wobble must not
        # fail the publish.
        try:
            announce_level_ups(db, user.club_id, body.system)
        except Exception as e:
            capture(e, kind="level_up_announce", system=body.system)
        # The channel has just been told; the players who don't read the
        # channel get the same news as a banner on their next page load.
        # Swallowed for the same reason as the two above: never fail a publish.
        try:
            from notices import notify_pairings_published

            notify_pairings_published(db, user.club_id, body.system, body.week)
            db.commit()
        except Exception as e:
            db.rollback()
            capture(e, kind="notices_on_publish", system=body.system)
    return {"ok": True, "published": body.published}


@router.post("/pairings/save")
def pairings_save(
    body: PairingSaveBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Write grid edits back to Pairing rows and their underlying Signup rows.

    Moving players between rows keeps every signup in exactly one place:
    - a player moved into a game has any BYE they were sitting on removed, so
      filling a BYE with someone else's waiting player is a single save;
    - a player moved out of a row and into nothing gets a BYE;
    - a player who would end up in two games is refused with a 409, and so is
      a player set against themselves or one not signed up for this week.
    Nothing is written unless the whole save is valid. Until 2026-09-15 none of
    this was checked, so a player could be double-booked or silently removed
    from the week.
    """
    _require_system_scope(body.system, user, db)

    week_signups = {
        su.id: su
        for su in db.exec(
            scoped(Signup, user.club_id)
            .where(Signup.week == body.week)
            .where(Signup.system == body.system)
        ).all()
    }
    moved_in: set[int] = set()
    moved_out: set[int] = set()
    # signup id -> ids of the rows this save moves them INTO
    placed_into: dict[int, set[int]] = {}

    changed = 0
    for row in body.rows:
        p = db.get(Pairing, row.id)
        if p is None:
            continue
        if p.week != body.week or p.system != body.system or p.club_id != user.club_id:
            continue

        old_ids = {sid for sid in (p.a_signup_id, p.b_signup_id) if sid is not None}
        new_a = row.a_signup_id if row.a_signup_id is not None else p.a_signup_id
        new_b = row.b_signup_id if row.b_signup_id is not None else p.b_signup_id
        # Only a CHANGED slot is validated: the grid sends every row back, and
        # an untouched row left pointing at a deleted signup must not block an
        # unrelated edit.
        for new_id, old_id in ((new_a, p.a_signup_id), (new_b, p.b_signup_id)):
            if new_id != old_id and new_id not in week_signups:
                raise HTTPException(
                    status_code=422,
                    detail="That player is not signed up for this week.",
                )
        if new_b is not None and new_a == new_b:
            raise HTTPException(
                status_code=422,
                detail=f"{week_signups[new_a].player_name if new_a in week_signups else 'A player'} cannot play themselves.",
            )
        p.a_signup_id = new_a
        p.b_signup_id = new_b
        new_ids = {sid for sid in (new_a, new_b) if sid is not None}
        moved_in |= new_ids - old_ids
        moved_out |= old_ids - new_ids
        for sid in new_ids - old_ids:
            placed_into.setdefault(sid, set()).add(p.id)

        # Faction — "— None —" sentinel → None
        def _clean_faction(f: Optional[str]) -> Optional[str]:
            if f in (None, "— None —", ""):
                return None
            return f

        a_faction = _clean_faction(row.a_faction)
        b_faction = _clean_faction(row.b_faction)

        # Everything below describes the GAME and is written to the pairing.
        # The signups are what the players asked for and are never written from
        # here: doing that rewrote both players' signup times whenever an admin
        # nudged a game (reported 16/09/2026). To change what somebody signed
        # up with, edit their signup (PATCH /admin/signups/{id}).
        #
        # Faction keeps "" for "explicitly none", since NULL now means "follow
        # the signup".
        p.a_faction = ("" if a_faction is None else a_faction) if p.a_signup_id else None
        p.b_faction = ("" if b_faction is None else b_faction) if p.b_signup_id else None

        # Vibe — per-side type if given, else the shared one. Blank leaves it to
        # the signups.
        a_type_raw = (row.a_type or "").strip()
        b_type_raw = (row.b_type or "").strip()
        shared_type = (row.type or "").strip()
        p.a_vibe = (a_type_raw or shared_type or None) if p.a_signup_id else None
        p.b_vibe = (b_type_raw or shared_type or None) if p.b_signup_id else None

        # ETA and points. Blank clears the override, so the game follows the
        # signup times again.
        p.eta = (row.eta or "").strip() or None
        pts_val: Optional[int] = None
        try:
            if row.points is not None and str(row.points).strip():
                pts_val = int(row.points)
        except (ValueError, TypeError):
            pts_val = None
        p.points = pts_val

        db.add(p)
        changed += 1

    if moved_in or moved_out:
        db.flush()
        rows = week_pairings.week_rows(db, user.club_id, body.system, body.week)

        def rows_with(sid: int) -> list:
            return [r for r in rows if sid in (r.a_signup_id, r.b_signup_id)]

        for sid in sorted(moved_in):
            mine = rows_with(sid)
            into = placed_into.get(sid, set())
            name = week_signups[sid].player_name
            if len([r for r in mine if r.id in into]) > 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"{name} has been put in more than one pairing. Pick one.",
                )
            already = [r for r in mine if r.id not in into]
            if any(r.b_signup_id is not None for r in already):
                raise HTTPException(
                    status_code=409,
                    detail=f"{name} is already in another game. Take them out of it first.",
                )
            # What is left in `already` is BYEs they were sitting on, which the
            # move replaces.
            for r in already:
                db.delete(r)
                rows = [x for x in rows if x.id != r.id]
        db.flush()
        for sid in sorted(moved_out):
            if sid in week_signups and not rows_with(sid):
                rows.append(week_pairings.add_bye(db, user.club_id, body.system, body.week, week_signups[sid]))

    db.commit()
    if moved_in or moved_out:
        _resync_venue_floor(db, user.club_id, body.system, body.week)
    return {"changed": changed}


@router.delete("/pairings")
def pairings_delete(
    body: DeletePairingsBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Delete specific pairings by ID, scoped to this week/system.

    A player whose signup still exists and who is left in no pairing at all
    gets a BYE, so they stay visible and can be re-arranged. Until 2026-09-15
    deleting a game here left both players with no game, no BYE and no place
    on the unpaired list.

    Deleting a BYE is refused when it is that player's only place in the week:
    it would put them back into exactly that state. Removing someone from the
    week is a force drop in the signups list, which also deletes their signup.
    A pairing whose signups no longer exist (left behind by the old drop code)
    can always be deleted.
    """
    _require_system_scope(body.system, user, db)

    targets: list[Pairing] = []
    for pid in body.ids:
        p = db.get(Pairing, pid)
        if p is None:
            continue
        if p.week != body.week or p.system != body.system or p.club_id != user.club_id:
            continue
        if p not in targets:
            targets.append(p)
    if not targets:
        return {"deleted": 0}

    target_ids = {p.id for p in targets}
    remaining = {
        sid
        for r in week_pairings.week_rows(db, user.club_id, body.system, body.week)
        if r.id not in target_ids
        for sid in (r.a_signup_id, r.b_signup_id)
        if sid is not None
    }
    affected = {sid for p in targets for sid in (p.a_signup_id, p.b_signup_id) if sid is not None}
    live = {
        su.id: su
        for su in db.exec(scoped(Signup, user.club_id).where(Signup.id.in_(affected))).all()
    } if affected else {}
    stranded = sorted(sid for sid in affected if sid in live and sid not in remaining)

    for p in targets:
        if p.b_signup_id is None and p.a_signup_id in stranded:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{live[p.a_signup_id].player_name} would be left with no game and no BYE. "
                    f"To take them out of this week, force drop their signup in the signups list."
                ),
            )

    for p in targets:
        db.delete(p)
    db.flush()
    for sid in stranded:
        week_pairings.add_bye(db, user.club_id, body.system, body.week, live[sid])
    db.commit()
    _resync_venue_floor(db, user.club_id, body.system, body.week)
    return {"deleted": len(targets)}


@router.post("/pairings/post-discord")
def pairings_post_discord(
    body: PairingsWeekBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Trigger the GitHub Actions workflow that renders this system/week's
    pairings as an image and posts it to that system's Discord channel.

    Fire-and-forget: a successful response means the workflow was queued, not
    that the Discord post has happened yet — installing matplotlib and
    rendering takes roughly 30-60 seconds in CI.
    """
    _require_system_scope(body.system, user, db)

    if not posting_enabled(db, user.club_id, body.system, "pairings"):
        return {"posted": False,
                "reason": "Pairings posts are switched off for this system. "
                          "Turn them back on above the pairings grid."}

    # The club slug is REQUIRED, not optional. Without it the workflow falls
    # back to resolving the single club running the system, which raises as
    # soon as two clubs share one — and Kill Team runs at both EGNWGC and The
    # Outpost, so this button queued a run that then failed in CI with nothing
    # to show for it. The caller always knows which club it is; say so.
    club = db.get(Club, user.club_id)
    if club is None:
        return {"posted": False, "reason": "club not found"}

    ok, reason = dispatch_workflow(
        PAIRINGS_IMAGE_WORKFLOW,
        {"system": body.system, "week": body.week, "club": club.slug},
    )
    if not ok:
        return {"posted": False, "reason": reason}

    return {"posted": True, "queued": True}


# ---------------------------------------------------------------------------
# Pairing weighting config — per-(club, system), self-service by that
# system's admin (same _require_system_scope gate as league config above).
# ---------------------------------------------------------------------------

def _pairing_config_row(cfg: PairingConfig) -> dict:
    return {
        "weight_mirror": cfg.weight_mirror,
        "weight_faction_group": cfg.weight_faction_group,
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
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """This club's pairing weighting config for one system. Returns the
    unsaved-defaults shape if the club hasn't customised it yet — same
    "defaults until saved" convention as league config."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    # As this club runs it, so a scenarios or points slider shows exactly when
    # the matcher will actually use it.
    config = effective_system(db, user.club_id, config)
    cfg = _get_pairing_config(db, user.club_id, config.id)
    return {
        "uses_scenarios": config.uses_scenarios,
        "uses_points": config.uses_points,
        # Whether this system HAS faction categories at all. The category
        # weight is inert without them, so the UI hides that slider rather
        # than offering a control that provably does nothing.
        "has_faction_groups": bool(resolved_faction_groups(config)),
        # Platform defaults shown as placeholders when the club hasn't overridden
        # the rematch windows.
        "default_recent_weeks": config.recent_weeks,
        "default_extended_weeks": config.extended_weeks,
        **_pairing_config_row(cfg),
    }


class PairingConfigBody(BaseModel):
    system: str
    weight_mirror: float = 5.0
    weight_faction_group: float = 1.0
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
    user: User = Depends(_require_any_admin),
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
