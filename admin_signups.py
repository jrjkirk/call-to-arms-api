"""Admin endpoints for a week's signups: list, add, edit and remove them.
Split out of admin.py; shared helpers still live there.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from auth import (
    get_session,
    require_admin,
)
from database import name_with_mention, scoped
from models import Player, Signup, User
from observability import capture
from system_overrides import effective_system
import week_pairings
from experience import summary as experience_summary
from signups import (
    EXPERIENCE_OPTIONS,
    _effective_vibe_config,
    _get_system_config,
    _require_system_enabled,
    _validate_week,
    _build_bye_discord_message,
    _get_all_byes,
    _post_webhook,
)

from admin import (
    _require_system_scope,
    _resync_venue_floor,
    APP_PUBLIC_URL,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_require_any_admin = require_admin


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
    capability (backfilled to The Old World, its original only home).

    Every field is read as this club runs the system (EffectiveSystem), so the
    editor offers the same fields as the signup form the club's players see."""
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    config = effective_system(db, club_id, config)
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    # As this club runs the system, the same rules the signup form applies.
    config = effective_system(db, user.club_id, config)
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
        su.standby_ok = bool(body.standby_ok) if (config.uses_standby and body.standby_ok is not None) else False

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
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Add a signup on behalf of a player (admin correction / manual entry).

    Unlike POST /signups, this does NOT upsert — a 409 is returned if the player
    already has a signup for this week/system so the admin uses PATCH instead.
    Discord signup webhook is NOT fired (avoid spurious 'X signed up' posts for
    admin corrections).
    Per-system defaults and normalisation match the regular POST /signups exactly,
    read as this club runs the system.

    Experience is DERIVED from games played, exactly as on POST /signups, and
    the request's `experience` is ignored. It used to be taken from the form,
    whose default was "New", so a veteran added by an admin was matched and
    shown as a newcomer. An admin can still correct it afterwards with PATCH.

    If the week already has pairings, the player is seated in them at once (a
    game against someone sitting out if there is a good match, a BYE
    otherwise), as a late signup is. When that gives a waiting player a game in
    a published week, the channel is told, because that player's night has
    changed.
    """
    config = _get_system_config(db, body.system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    config = effective_system(db, user.club_id, config)

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
            detail="This player is already signed up for this week. Edit their existing row instead.",
        )

    uses_points, uses_scenarios, allows_demo = config.uses_points, config.uses_scenarios, config.allows_demo
    default_points, max_points = config.default_points, config.max_points
    vibe_options, default_vibe = _effective_vibe_config(db, user.club_id, config)

    faction = body.faction
    if faction in (None, "", "— None —"):
        faction = None

    experience = experience_summary(db, user.club_id, player.id, body.system)["tier"]
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

    # The signup is already committed; a failure seating it is reported rather
    # than returned as an error for a row that does exist.
    partner = None
    seated = False
    try:
        if week_pairings.is_paired(db, user.club_id, body.system, week):
            partner = week_pairings.seat(db, user.club_id, body.system, week, su)
            db.commit()
            seated = True
    except Exception as e:
        db.rollback()
        partner = None
        capture(e, kind="admin_signup_seat", club_id=user.club_id, system=body.system)
    db.refresh(su)
    if seated:
        if partner is not None and week_pairings.is_published(db, user.club_id, body.system, week):
            _post_webhook(
                db, user.club_id, body.system,
                _build_bye_discord_message(
                    db,
                    header=f"➕ {name_with_mention(db, su.player_name, su.player_id)} has been added to this week's session.",
                    moved=[(su, partner)],
                    all_byes=_get_all_byes(db, body.system, week, user.club_id),
                    app_url=APP_PUBLIC_URL,
                ),
            )
            try:
                from notices import notify_game_found

                notify_game_found(db, user.club_id, body.system, week, partner, su.player_name)
                db.commit()
            except Exception as e:
                db.rollback()
                capture(e, kind="notices_on_admin_signup", system=body.system)
        _resync_venue_floor(db, user.club_id, body.system, week)
        db.refresh(su)
    return _signup_row(su)


@router.delete("/signups/{signup_id}")
def admin_signup_delete(
    signup_id: int,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Force-drop a signup, bypassing the PublishState check that blocks player self-drops.

    Takes the player out of every pairing they are in, the same way a player
    dropping themselves does (see week_pairings.release). Once the week has
    pairings their opponent is seated again straight away; before that, a
    prearranged opponent returns to the pool. Until 2026-09-15 only
    prearranged pairings were removed, so a generated game was left pointing
    at a deleted signup and, after publish, the opponent was stranded with no
    BYE and no word.

    The plain "X dropped" webhook is NOT fired (avoid spurious posts for admin
    corrections). The one exception is a published week where removing the
    player changed someone else's game: that player needs to hear it.
    """
    su = db.get(Signup, signup_id)
    if su is None or su.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Signup not found.")

    _require_system_scope(su.system, user, db)

    system, week = su.system, su.week
    name, player_id = su.player_name, su.player_id
    published = week_pairings.is_published(db, user.club_id, system, week)
    paired = published or week_pairings.is_paired(db, user.club_id, system, week)

    moved = week_pairings.release(db, user.club_id, system, week, {signup_id}, reseat=paired)

    db.delete(su)
    db.commit()

    if published and moved:
        _post_webhook(
            db, user.club_id, system,
            _build_bye_discord_message(
                db,
                header=f"❌ {name_with_mention(db, name, player_id)} has been removed from this week's session.",
                moved=moved,
                all_byes=_get_all_byes(db, system, week, user.club_id),
                app_url=APP_PUBLIC_URL,
            ),
        )
        try:
            from notices import notify_opponent_dropped

            notify_opponent_dropped(db, user.club_id, system, week, moved, name)
            db.commit()
        except Exception as e:
            db.rollback()
            capture(e, kind="notices_on_admin_drop", system=system)
    if paired:
        _resync_venue_floor(db, user.club_id, system, week)
    return {"ok": True}
