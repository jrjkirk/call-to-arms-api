"""Admin endpoints: role management and scoped access.

Permission model:
- Super-admin: user.is_super_admin == True. Set by SQL, never via this API.
  Can do everything and is the only role that can appoint/remove scope admins.
- Scope admin: a row in admin_roles (user_id, scope). One row per scope held.
"""
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    VALID_SCOPES,
    admin_scopes,
    current_user,
    get_session,
    require_super_admin,
    require_user,
)
from models import AdminRole, LeagueResult, PairingBlock, Pairing, Player, PublishState, Signup, User
from pairings_engine import generate

DISCORD_TOW_PAIRINGS_WEBHOOK_URL = os.environ.get("DISCORD_TOW_PAIRINGS_WEBHOOK_URL", "")
DISCORD_HH_PAIRINGS_WEBHOOK_URL = os.environ.get("DISCORD_HH_PAIRINGS_WEBHOOK_URL", "")
DISCORD_KT_PAIRINGS_WEBHOOK_URL = os.environ.get("DISCORD_KT_PAIRINGS_WEBHOOK_URL", "")

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
        "scopes": sorted(scopes),
    }


@router.get("/roles")
def list_roles(
    _: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """All current admin_roles rows joined to user info, plus super-admin list."""
    role_rows = db.exec(
        select(AdminRole, User)
        .join(User, User.id == AdminRole.user_id)
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
        select(User).where(User.is_super_admin == True).order_by(User.discord_name)
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
    _: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Active users with a linked player — the candidate list for the appoint UI."""
    users = db.exec(
        select(User).where(User.player_id.isnot(None))
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
    _: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Idempotent: insert (user_id, scope) if not already present."""
    if body.scope not in VALID_SCOPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scope. Must be one of: {sorted(VALID_SCOPES)}",
        )

    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    existing = db.exec(
        select(AdminRole)
        .where(AdminRole.user_id == body.user_id)
        .where(AdminRole.scope == body.scope)
    ).first()

    if existing is None:
        db.add(AdminRole(user_id=body.user_id, scope=body.scope))
        db.commit()

    return {"ok": True}


@router.delete("/roles")
def remove_role(
    user_id: int,
    scope: str,
    _: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Delete the (user_id, scope) row if present."""
    row = db.exec(
        select(AdminRole)
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
    scope: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Scoped read — 403 unless the caller holds that scope."""
    if scope not in VALID_SCOPES:
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if scope not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")

    players = db.exec(
        select(Player).where(Player.active == True).order_by(Player.name)
    ).all()
    return [{"id": p.id, "name": p.name, "active": p.active} for p in players]


# ---------------------------------------------------------------------------
# Pairing blocks (global — no system column, canonical low < high storage)
# ---------------------------------------------------------------------------

@router.get("/blocks/players")
def block_players(
    _: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """All active players — used to populate the add-block form dropdowns."""
    players = db.exec(
        select(Player).where(Player.active == True).order_by(Player.name)
    ).all()
    return [{"id": p.id, "name": p.name} for p in players]


@router.get("/blocks")
def list_blocks(
    _: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """All pairing blocks, enriched with player names, sorted A→B."""
    block_rows = db.exec(select(PairingBlock)).all()

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
    _: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Insert a canonical (low, high) block. Idempotent; updates note if block exists."""
    if body.player_a_id == body.player_b_id:
        raise HTTPException(status_code=422, detail="Cannot block a player from themselves.")

    low, high = sorted([body.player_a_id, body.player_b_id])
    note = (body.note or "").strip() or None

    existing = db.exec(
        select(PairingBlock)
        .where(PairingBlock.player_a_id == low)
        .where(PairingBlock.player_b_id == high)
    ).first()

    if existing is not None:
        if note:
            existing.note = note
            db.add(existing)
            db.commit()
        return {"ok": True, "created": False}

    db.add(PairingBlock(player_a_id=low, player_b_id=high, note=note))
    db.commit()
    return {"ok": True, "created": True}


@router.delete("/blocks")
def remove_block(
    player_a_id: int,
    player_b_id: int,
    _: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Delete the canonical (low, high) block if it exists."""
    low, high = sorted([player_a_id, player_b_id])
    row = db.exec(
        select(PairingBlock)
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
    if scope not in VALID_SCOPES:
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if scope not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")

    if scope == "League":
        rows = db.exec(
            select(LeagueResult).order_by(LeagueResult.id.desc()).limit(100)
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
        select(Pairing)
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
    if a_vibe and b_vibe:
        if a_vibe.strip().lower() == b_vibe.strip().lower():
            return a_vibe
        return None
    return a_vibe or b_vibe or None


def _eta_show(a_su: Optional[Signup], b_su: Optional[Signup]) -> Optional[str]:
    a_eta = a_su.eta if a_su else None
    b_eta = b_su.eta if b_su else None
    if a_eta and b_eta:
        return max(a_eta, b_eta)
    return a_eta or b_eta


def _pts_show(a_su: Optional[Signup], b_su: Optional[Signup]) -> Optional[str]:
    a_pts = a_su.points if a_su else None
    b_pts = b_su.points if b_su else None
    if a_pts and b_pts:
        return str(a_pts) if a_pts == b_pts else f"{min(a_pts, b_pts)}-{max(a_pts, b_pts)}"
    val = a_pts or b_pts
    return str(val) if val else None


def _build_display_row(
    row_id: Optional[int],
    a_signup_id: int,
    b_signup_id: Optional[int],
    a_faction: Optional[str],
    b_faction: Optional[str],
    prearranged: bool,
    signups_by_id: dict,
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
        "points": _pts_show(a_su, b_su),
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


def _pairing_rows_to_display(pairings: list, signups_by_id: dict) -> list:
    result = []
    for p in pairings:
        a_faction = p.a_faction or (signups_by_id.get(p.a_signup_id, None) and signups_by_id[p.a_signup_id].faction)
        b_faction = None
        if p.b_signup_id:
            b_faction = p.b_faction or (signups_by_id.get(p.b_signup_id, None) and signups_by_id[p.b_signup_id].faction)
        result.append(_build_display_row(
            p.id, p.a_signup_id, p.b_signup_id,
            a_faction, b_faction,
            p.prearranged, signups_by_id,
        ))
    return result


def _dicts_to_display(dicts: list, signups_by_id: dict) -> list:
    return [
        _build_display_row(
            None,
            d["a_signup_id"], d.get("b_signup_id"),
            d.get("a_faction"), d.get("b_faction"),
            False, signups_by_id,
        )
        for d in dicts
    ]


def _pairings_webhook_url(system: str) -> str:
    if system == "The Old World":
        return DISCORD_TOW_PAIRINGS_WEBHOOK_URL
    if system == "The Horus Heresy":
        return DISCORD_HH_PAIRINGS_WEBHOOK_URL
    if system == "Kill Team":
        return DISCORD_KT_PAIRINGS_WEBHOOK_URL
    return ""


def _require_system_scope(system: str, user: User, db: Session) -> None:
    if system not in VALID_SCOPES:
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if system not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{system}' required.")


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
        select(Signup)
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
        select(Pairing)
        .where(Pairing.week == body.week)
        .where(Pairing.system == body.system)
        .where(Pairing.prearranged == True)
        .order_by(Pairing.id)
    ).all()

    proposed_dicts = generate(db, body.week, body.system, persist=False)

    all_rows = list(prearranged) + proposed_dicts
    signups_by_id = _collect_signups_for_rows(all_rows, db)

    display = _pairing_rows_to_display(prearranged, signups_by_id) + \
              _dicts_to_display(proposed_dicts, signups_by_id)

    return {"rows": display, "preview": True}


@router.post("/pairings/generate")
def pairings_generate(
    body: PairingsWeekBody,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Delete existing pending (non-prearranged) pairings, then generate and persist new ones."""
    _require_system_scope(body.system, user, db)

    old = db.exec(
        select(Pairing)
        .where(Pairing.week == body.week)
        .where(Pairing.system == body.system)
        .where(Pairing.status == "pending")
        .where(Pairing.prearranged != True)
    ).all()
    for p in old:
        db.delete(p)
    # autoflush will sync deletes before generate queries

    new_pairings = generate(db, body.week, body.system, persist=True)

    signups_by_id = _collect_signups_for_rows(new_pairings, db)
    display = _pairing_rows_to_display(new_pairings, signups_by_id)
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
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .order_by(Pairing.id)
    ).all()

    gate = db.exec(
        select(PublishState)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()
    published = gate.published if gate else False

    signups_by_id = _collect_signups_for_rows(rows, db)
    display = _pairing_rows_to_display(rows, signups_by_id)
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
        select(PublishState)
        .where(PublishState.week == body.week)
        .where(PublishState.system == body.system)
    ).first()

    if gate is None:
        gate = PublishState(week=body.week, system=body.system, published=body.published)
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
        if p.week != body.week or p.system != body.system:
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
        if p.week != body.week or p.system != body.system:
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
    """Build the public pairing list and POST it to the system Discord webhook.

    Note: posts a plain-text table (the original app posts an image; text is an
    acceptable simplification for v1).
    Webhooks are intentionally unset so nothing posts during development.
    """
    _require_system_scope(body.system, user, db)

    webhook_url = _pairings_webhook_url(body.system)
    if not webhook_url:
        return {"posted": False, "reason": "no webhook"}

    rows = db.exec(
        select(Pairing)
        .where(Pairing.week == body.week)
        .where(Pairing.system == body.system)
        .order_by(Pairing.id)
    ).all()

    signup_ids = {p.a_signup_id for p in rows} | {
        p.b_signup_id for p in rows if p.b_signup_id
    }
    signups_by_id: dict = {}
    if signup_ids:
        su_rows = db.exec(select(Signup).where(Signup.id.in_(signup_ids))).all()
        signups_by_id = {s.id: s for s in su_rows}

    lines = [f"**{body.system} — {body.week}**\n"]
    for p in rows:
        a_su = signups_by_id.get(p.a_signup_id)
        b_su = signups_by_id.get(p.b_signup_id) if p.b_signup_id else None
        a_name = a_su.player_name if a_su else f"#{p.a_signup_id}"
        a_fac = p.a_faction or (a_su.faction if a_su else None) or "—"
        if b_su:
            b_name = b_su.player_name
            b_fac = p.b_faction or b_su.faction or "—"
            lines.append(f"**{a_name}** ({a_fac}) vs **{b_name}** ({b_fac})")
        else:
            lines.append(f"**{a_name}** — BYE")

    content = "\n".join(lines)
    try:
        httpx.post(webhook_url, json={"content": content}, timeout=8.0)
    except Exception:
        return {"posted": False, "reason": "webhook request failed"}

    return {"posted": True}
