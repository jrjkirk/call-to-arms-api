"""Admin endpoints: role management and scoped access.

Permission model:
- Super-admin: user.is_super_admin == True. Set by SQL, never via this API.
  Can do everything and is the only role that can appoint/remove scope admins.
- Scope admin: a row in admin_roles (user_id, scope). One row per scope held.
"""
from typing import Optional

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
from models import AdminRole, LeagueResult, PairingBlock, Pairing, Player, Signup, User

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
