"""Signup endpoints: the Call to Arms form.

Semantics are a faithful port of the Streamlit app:
- One effective signup per player/week/system. Submitting again updates the
  newest existing row and deletes any older duplicates.
- Dropping out is blocked once pairings are published for that week/system.
- Dropping out also deletes any PREARRANGED pairing involving the dropper
  (the opponent's signup stays, so they get re-pooled next pairing run).
- Discord webhooks fire on brand-new signups and on drops, per-system,
  and silently no-op when the webhook URL env var isn't set.
"""
import os
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, SQLModel, select

from database import get_session
from models import Signup, Pairing, PublishState, Player, User
from auth import admin_scopes, require_user

router = APIRouter(prefix="/signups", tags=["signups"])

SYSTEMS = {"The Old World", "The Horus Heresy", "Kill Team"}
EXPERIENCE_OPTIONS = {"New", "Some", "Veteran"}
TOW_VIBES = {"Casual", "Competitive", "Escalation", "Intro", "Either"}
HH_VIBES = {"Standard", "Intro"}
SCENARIO_OPTIONS = {"Open Battle", "Weekly Scenario"}

# Per-system signup notification webhooks. Same env var names as the
# original app, so the values can be copied straight across at cutover.
DISCORD_SIGNUP_WEBHOOK_URL = os.environ.get("DISCORD_SIGNUP_WEBHOOK_URL", "")
DISCORD_HH_SIGNUP_WEBHOOK_URL = os.environ.get("DISCORD_HH_SIGNUP_WEBHOOK_URL", "")
DISCORD_KT_SIGNUP_WEBHOOK_URL = os.environ.get("DISCORD_KT_SIGNUP_WEBHOOK_URL", "")
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")


class SignupIn(SQLModel):
    """Request body for POST /signups. Player identity comes from the session."""
    system: str
    week: str
    faction: Optional[str] = None
    points: Optional[int] = None
    eta: Optional[str] = None
    experience: str = "New"
    vibe: str = "Casual"
    standby_ok: bool = False
    scenario: Optional[str] = None
    can_demo: bool = False


class SwapIn(SQLModel):
    """Request body for POST /signups/swap."""
    system: str
    week: str
    opponent_player_id: int
    player_1_id: Optional[int] = None


class PrearrangedGameIn(SQLModel):
    """Request body for POST /signups/prearranged."""
    system: str
    week: str
    player_a_id: int
    player_b_id: int
    faction_a: Optional[str] = None
    faction_b: Optional[str] = None
    eta: Optional[str] = None
    vibe: str = "Casual"
    points: Optional[int] = None


def _require_linked_player(user: User, db: Session) -> Player:
    if user.player_id is None:
        raise HTTPException(status_code=400, detail="No linked player profile — claim your profile first.")
    player = db.get(Player, user.player_id)
    if player is None or not player.active:
        raise HTTPException(status_code=400, detail="Linked player profile not found.")
    return player


def _validate_week(week: str) -> str:
    week = week.strip()
    try:
        datetime.strptime(week, "%d/%m/%Y")
    except ValueError:
        raise HTTPException(status_code=422, detail="Week must be in DD/MM/YYYY format.")
    return week


def _signup_webhook_for_system(system: str) -> str:
    if system == "The Old World":
        return DISCORD_SIGNUP_WEBHOOK_URL
    if system == "The Horus Heresy":
        return DISCORD_HH_SIGNUP_WEBHOOK_URL
    if system == "Kill Team":
        return DISCORD_KT_SIGNUP_WEBHOOK_URL
    return ""


def _signup_count_phrase_for_system(system: str) -> str:
    if system == "The Horus Heresy":
        return "HH session signups"
    if system == "The Old World":
        return "TOW signups this week"
    if system == "Kill Team":
        return "KT signups this week"
    return f"{system} signups this week"


def _signup_count(db: Session, system: str, week: str) -> int:
    """Distinct players signed up for this week/system (latest row per player wins)."""
    rows = db.exec(
        select(Signup)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at.desc())
    ).all()
    seen = set()
    for s in rows:
        seen.add(s.player_id if s.player_id is not None else id(s))
    return len(seen)


def _post_webhook(system: str, content: str) -> None:
    """Fire-and-forget Discord post. Never breaks the request on failure."""
    url = _signup_webhook_for_system(system)
    if not url:
        return
    try:
        httpx.post(url, json={"content": content}, timeout=5.0)
    except Exception:
        pass


def _post_discord_signup(db: Session, player_name: str, faction: Optional[str], vibe: Optional[str], system: str, week: str) -> None:
    faction_label = faction or "Unknown faction"
    vibe_label = vibe or "Unknown vibe"
    count = _signup_count(db, system, week)
    phrase = _signup_count_phrase_for_system(system)
    _post_webhook(system, f"📝 **{player_name}** signed up — ⚔️ {faction_label} • 🎭 {vibe_label}\n📊 {phrase}: {count}")


def _post_discord_drop(db: Session, player_name: str, faction: Optional[str], vibe: Optional[str], system: str, week: str) -> None:
    faction_label = faction or "Unknown faction"
    vibe_label = vibe or "Unknown vibe"
    count = _signup_count(db, system, week)
    phrase = _signup_count_phrase_for_system(system)
    _post_webhook(system, f"❌ **{player_name}** dropped — ⚔️ {faction_label} • 🎭 {vibe_label}\n📊 {phrase}: {count}")


def _get_all_byes(db: Session, system: str, week: str) -> list[dict]:
    """Return all current BYE players for this week/system, ordered by player name."""
    pub = db.exec(
        select(PublishState)
        .where(PublishState.system == system)
        .where(PublishState.week == week)
    ).first()
    if not pub or not pub.published:
        return []
    bye_pairings = db.exec(
        select(Pairing)
        .where(Pairing.system == system)
        .where(Pairing.week == week)
        .where(Pairing.b_signup_id.is_(None))
    ).all()
    if not bye_pairings:
        return []
    signup_ids = [p.a_signup_id for p in bye_pairings]
    signups = db.exec(select(Signup).where(Signup.id.in_(signup_ids))).all()
    signups_by_id = {s.id: s for s in signups}
    result = []
    for p in bye_pairings:
        su = signups_by_id.get(p.a_signup_id)
        if su:
            result.append({
                "player_name": su.player_name,
                "signup_id": p.a_signup_id,
                "is_new": False,
            })
    result.sort(key=lambda x: x["player_name"])
    return result


def _build_bye_discord_message(
    header: str,
    newly_displaced_names: list[str],
    all_byes: list[dict],
    app_url: str,
) -> str:
    """Build a consistent Discord message for swap/drop events."""
    if not all_byes:
        return f"{header}\n\n➡️ {app_url}" if app_url else header
    lines = [header, "", "⚠️ The following players are now without an opponent this week:"]
    for bye in all_byes:
        suffix = " (existing bye)" if not bye["is_new"] else ""
        lines.append(f"• {bye['player_name']}{suffix}")
    lines.append("")
    lines.append(f"Head to the app to re-arrange your game: {app_url}")
    return "\n".join(lines)


@router.get("/mine")
def my_signup(
    system: str,
    week: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Return the user's signup for this exact week ('current') and their most
    recent signup for this system across any week ('last', used for prefill)."""
    player = _require_linked_player(user, db)
    week = _validate_week(week)

    current = db.exec(
        select(Signup)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .where(Signup.player_id == player.id)
        .order_by(Signup.id.desc())
    ).first()

    last = db.exec(
        select(Signup)
        .where(Signup.system == system)
        .where(Signup.player_id == player.id)
        .order_by(Signup.id.desc())
    ).first()

    return {"current": current, "last": last}


@router.post("")
def submit_signup(
    body: SignupIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    player = _require_linked_player(user, db)

    if body.system not in SYSTEMS:
        raise HTTPException(status_code=422, detail="Unknown system.")
    week = _validate_week(body.week)

    is_hh = body.system == "The Horus Heresy"
    is_kt = body.system == "Kill Team"

    # Normalise exactly like the original form does
    faction = body.faction
    if faction in (None, "", "— None —"):
        faction = None

    experience = body.experience if body.experience in EXPERIENCE_OPTIONS else "New"

    if is_kt:
        vibe = "Standard"
        points = 0
        scenario = None
        can_demo = False
    elif is_hh:
        vibe = body.vibe if body.vibe in HH_VIBES else "Standard"
        points = max(0, min(int(body.points or 3000), 10000))
        scenario = None
        can_demo = bool(body.can_demo)
    else:
        vibe = body.vibe if body.vibe in TOW_VIBES else "Casual"
        points = max(0, min(int(body.points or 2000), 10000))
        scenario = body.scenario if body.scenario in SCENARIO_OPTIONS else "Open Battle"
        can_demo = bool(body.can_demo)

    eta = (body.eta or "").strip() or None

    # Upsert: update the newest existing row, delete older duplicates
    existing = db.exec(
        select(Signup)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == player.id)
        .order_by(Signup.id.desc())
    ).all()

    created = not bool(existing)
    if existing:
        su = existing[0]
        for dup in existing[1:]:
            db.delete(dup)
        su.player_name = player.name
        su.faction = faction
        su.points = points
        su.eta = eta
        su.experience = experience
        su.vibe = vibe
        su.standby_ok = bool(body.standby_ok)
        su.tnt_ok = False
        su.scenario = scenario
        su.can_demo = can_demo
        db.add(su)
    else:
        su = Signup(
            week=week, system=body.system,
            player_id=player.id, player_name=player.name,
            faction=faction, points=points, eta=eta,
            experience=experience, vibe=vibe,
            standby_ok=bool(body.standby_ok), tnt_ok=False,
            scenario=scenario, can_demo=can_demo,
        )
        db.add(su)

    db.commit()
    db.refresh(su)

    if created:
        _post_discord_signup(db, player.name, faction, vibe, body.system, week)

    return {"ok": True, "created": created, "signup": su}


@router.delete("/mine")
def drop_signup(
    system: str,
    week: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    player = _require_linked_player(user, db)
    week = _validate_week(week)

    gate = db.exec(
        select(PublishState)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()

    if gate and gate.published:
        # Post-publish drop: reroute opponent to a BYE pairing, delete our pairing + signup
        rows = db.exec(
            select(Signup)
            .where(Signup.week == week)
            .where(Signup.system == system)
            .where(Signup.player_id == player.id)
        ).all()
        if not rows:
            return {"ok": True, "dropped": False}

        my_ids = {s.id for s in rows}

        pairing = db.exec(
            select(Pairing)
            .where(Pairing.week == week)
            .where(Pairing.system == system)
            .where((Pairing.a_signup_id.in_(my_ids)) | (Pairing.b_signup_id.in_(my_ids)))
        ).first()

        opponent_name: Optional[str] = None
        if pairing:
            if pairing.b_signup_id is not None:
                opponent_signup_id = (
                    pairing.b_signup_id if pairing.a_signup_id in my_ids
                    else pairing.a_signup_id
                )
                opponent_signup = db.get(Signup, opponent_signup_id)
                if opponent_signup:
                    opponent_name = opponent_signup.player_name
                    db.add(Pairing(
                        week=week, system=system,
                        a_signup_id=opponent_signup_id, b_signup_id=None,
                        status="pending", prearranged=False,
                        a_faction=opponent_signup.faction, b_faction=None,
                    ))
            db.delete(pairing)

        for s in rows:
            db.delete(s)

        db.commit()

        all_byes = _get_all_byes(db, system, week)
        newly_displaced_names = [opponent_name] if opponent_name else []
        for bye in all_byes:
            if bye["player_name"] in newly_displaced_names:
                bye["is_new"] = True
        content = _build_bye_discord_message(
            header=f"❌ **{player.name}** has dropped out of this week's session.",
            newly_displaced_names=newly_displaced_names,
            all_byes=all_byes,
            app_url=APP_PUBLIC_URL,
        )
        _post_webhook(system, content)

        return {"ok": True, "dropped": True, "published": True}

    # Pre-publish drop path (unchanged)
    rows = db.exec(
        select(Signup)
        .where(Signup.week == week)
        .where(Signup.system == system)
        .where(Signup.player_id == player.id)
    ).all()
    if not rows:
        return {"ok": True, "dropped": False}

    ref = rows[0]
    ref_faction, ref_vibe = ref.faction, ref.vibe
    my_ids = {s.id for s in rows}

    # Delete any prearranged pairing involving the dropper; opponent's signup stays
    prearranged = db.exec(
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .where(Pairing.prearranged == True)
        .where((Pairing.a_signup_id.in_(my_ids)) | (Pairing.b_signup_id.in_(my_ids)))
    ).all()
    for p in prearranged:
        db.delete(p)

    for s in rows:
        db.delete(s)

    db.commit()

    _post_discord_drop(db, player.name, ref_faction, ref_vibe, system, week)

    return {"ok": True, "dropped": True}


@router.post("/prearranged")
def submit_prearranged(
    body: PrearrangedGameIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    # 1. System and week
    if body.system not in SYSTEMS:
        raise HTTPException(status_code=422, detail="Unknown system.")
    week = _validate_week(body.week)

    # 2. Players must differ
    if body.player_a_id == body.player_b_id:
        raise HTTPException(status_code=422, detail="Player A and Player B must be different.")

    # 3. Both players must exist and be active
    pa = db.get(Player, body.player_a_id)
    if pa is None or not pa.active:
        raise HTTPException(status_code=404, detail="Player A not found.")
    pb = db.get(Player, body.player_b_id)
    if pb is None or not pb.active:
        raise HTTPException(status_code=404, detail="Player B not found.")

    # 4. Both factions must be set
    faction_a = body.faction_a if body.faction_a not in (None, "", "— None —") else None
    faction_b = body.faction_b if body.faction_b not in (None, "", "— None —") else None
    if faction_a is None or faction_b is None:
        raise HTTPException(status_code=422, detail="Please pick a faction for both players.")

    # 5. Conflict check: neither player may already be signed up this week/system
    conflicts = db.exec(
        select(Signup)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id.in_([body.player_a_id, body.player_b_id]))
    ).all()
    if conflicts:
        names = sorted({s.player_name for s in conflicts})
        raise HTTPException(
            status_code=409,
            detail=f"Already signed up: {', '.join(names)}. They must drop first before being part of a pre-arranged game.",
        )

    # Normalise per system
    is_kt = body.system == "Kill Team"
    is_hh = body.system == "The Horus Heresy"

    if is_kt:
        vibe = "Standard"
        points = None
    elif is_hh:
        vibe = body.vibe if body.vibe in HH_VIBES else "Standard"
        points = max(0, min(int(body.points or 3000), 10000))
    else:
        vibe = body.vibe if body.vibe in TOW_VIBES else "Casual"
        points = max(0, min(int(body.points or 2000), 10000))

    eta = (body.eta or "").strip() or None

    # Create both signups and pairing in one transaction
    su_a = Signup(
        week=week, system=body.system,
        player_id=pa.id, player_name=pa.name,
        faction=faction_a, points=points, eta=eta,
        experience="New", vibe=vibe,
        standby_ok=False, tnt_ok=False,
        scenario=None, can_demo=False,
    )
    su_b = Signup(
        week=week, system=body.system,
        player_id=pb.id, player_name=pb.name,
        faction=faction_b, points=points, eta=eta,
        experience="New", vibe=vibe,
        standby_ok=False, tnt_ok=False,
        scenario=None, can_demo=False,
    )
    db.add(su_a)
    db.add(su_b)
    db.flush()

    pairing = Pairing(
        week=week, system=body.system,
        a_signup_id=su_a.id, b_signup_id=su_b.id,
        status="pending",
        a_faction=faction_a, b_faction=faction_b,
        prearranged=True,
    )
    db.add(pairing)
    db.commit()
    db.refresh(su_a)
    db.refresh(su_b)
    db.refresh(pairing)

    try:
        count = _signup_count(db, body.system, week)
        phrase = _signup_count_phrase_for_system(body.system)
        detail_parts = [f"🎭 {vibe}"]
        if eta:
            detail_parts.append(f"⏰ {eta}")
        if points is not None:
            detail_parts.append(f"🛡️ {points} pts")
        detail_line = " • ".join(detail_parts)
        content = (
            f"🤝 **Pre-Arranged Game**\n"
            f"⚔️ **{pa.name}** ({faction_a}) vs **{pb.name}** ({faction_b})\n"
            f"{detail_line}\n"
            f"📊 {phrase}: {count}"
        )
        _post_webhook(body.system, content)
    except Exception:
        pass

    return {"ok": True, "signup_a": su_a, "signup_b": su_b, "pairing": pairing}


@router.post("/swap")
def swap_signups(
    body: SwapIn,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    week = _validate_week(body.week)

    # 1. Pairings must be published
    gate = db.exec(
        select(PublishState)
        .where(PublishState.week == week)
        .where(PublishState.system == body.system)
    ).first()
    if not gate or not gate.published:
        raise HTTPException(status_code=422, detail="Pairings are not published for this week.")

    # 2. Resolve player X.  Admins may supply player_1_id to act on behalf of
    #    any signed-up player; regular players are always player X themselves.
    if body.player_1_id is not None:
        if body.system not in admin_scopes(user, db):
            raise HTTPException(status_code=403, detail=f"Admin access for '{body.system}' required.")
        x_player_id = body.player_1_id
    else:
        player = _require_linked_player(user, db)
        x_player_id = player.id

    # 3. Find X signup
    x_signup = db.exec(
        select(Signup)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == x_player_id)
        .order_by(Signup.id.desc())
    ).first()
    if x_signup is None:
        detail = "Player 1 is not signed up for this week." if body.player_1_id is not None else "You are not signed up for this week."
        raise HTTPException(status_code=422, detail=detail)

    # 4. Must be different players
    if body.opponent_player_id == x_player_id:
        raise HTTPException(status_code=422, detail="Cannot swap with yourself.")

    # 4. Find Y (target player) signup
    y_signup = db.exec(
        select(Signup)
        .where(Signup.week == week)
        .where(Signup.system == body.system)
        .where(Signup.player_id == body.opponent_player_id)
        .order_by(Signup.id.desc())
    ).first()
    if y_signup is None:
        raise HTTPException(status_code=422, detail="That player is not signed up for this week.")

    # 5. Find X's current pairing; capture X's old opponent signup_id
    x_pairing = db.exec(
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == body.system)
        .where((Pairing.a_signup_id == x_signup.id) | (Pairing.b_signup_id == x_signup.id))
    ).first()

    z_signup_id: Optional[int] = None
    if x_pairing:
        z_signup_id = (
            x_pairing.b_signup_id if x_pairing.a_signup_id == x_signup.id
            else x_pairing.a_signup_id
        )

    # 6. Find Y's current pairing; capture Y's old opponent signup_id
    y_pairing = db.exec(
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == body.system)
        .where((Pairing.a_signup_id == y_signup.id) | (Pairing.b_signup_id == y_signup.id))
    ).first()

    w_signup_id: Optional[int] = None
    if y_pairing:
        w_signup_id = (
            y_pairing.b_signup_id if y_pairing.a_signup_id == y_signup.id
            else y_pairing.a_signup_id
        )

    # 7. Edge case: X and Y are already paired with each other
    if x_pairing and y_pairing and x_pairing.id == y_pairing.id:
        return {"ok": True, "already_paired": True}

    # 8. Capture displaced player data before deleting
    z_signup: Optional[Signup] = db.get(Signup, z_signup_id) if z_signup_id is not None else None
    w_signup: Optional[Signup] = db.get(Signup, w_signup_id) if w_signup_id is not None else None

    # 9. Delete X's and Y's current pairings
    if x_pairing:
        db.delete(x_pairing)
    if y_pairing:
        db.delete(y_pairing)

    # 10. Create new X vs Y prearranged pairing
    db.add(Pairing(
        week=week, system=body.system,
        a_signup_id=x_signup.id, b_signup_id=y_signup.id,
        status="pending", prearranged=True,
        a_faction=x_signup.faction, b_faction=y_signup.faction,
    ))

    # 11. Create BYE pairings for each displaced real player
    if z_signup is not None:
        db.add(Pairing(
            week=week, system=body.system,
            a_signup_id=z_signup_id, b_signup_id=None,
            status="pending", prearranged=False,
            a_faction=z_signup.faction, b_faction=None,
        ))
    if w_signup is not None:
        db.add(Pairing(
            week=week, system=body.system,
            a_signup_id=w_signup_id, b_signup_id=None,
            status="pending", prearranged=False,
            a_faction=w_signup.faction, b_faction=None,
        ))

    # 12. Commit
    db.commit()

    # 13. Discord
    x_name = x_signup.player_name
    y_name = y_signup.player_name
    displaced = []
    if z_signup is not None:
        displaced.append({"player_id": z_signup.player_id, "player_name": z_signup.player_name})
    if w_signup is not None:
        displaced.append({"player_id": w_signup.player_id, "player_name": w_signup.player_name})

    all_byes = _get_all_byes(db, body.system, week)
    z_name = z_signup.player_name if z_signup is not None else None
    w_name = w_signup.player_name if w_signup is not None else None
    newly_displaced_names = [name for name in [z_name, w_name] if name]
    for bye in all_byes:
        if bye["player_name"] in newly_displaced_names:
            bye["is_new"] = True
    content = _build_bye_discord_message(
        header=f"🔀 **{x_name}** and **{y_name}** have re-arranged their games!",
        newly_displaced_names=newly_displaced_names,
        all_byes=all_byes,
        app_url=APP_PUBLIC_URL,
    )
    _post_webhook(body.system, content)

    # 14. Return
    return {
        "ok": True,
        "new_pairing": {
            "x_name": x_name,
            "y_name": y_name,
            "x_faction": x_signup.faction,
            "y_faction": y_signup.faction,
        },
        "displaced": displaced,
    }


@router.get("/unpaired")
def get_unpaired(
    system: str,
    week: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    week = _validate_week(week)

    gate = db.exec(
        select(PublishState)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()
    if not gate or not gate.published:
        return []

    bye_pairings = db.exec(
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .where(Pairing.b_signup_id.is_(None))
    ).all()

    result = []
    for p in bye_pairings:
        signup = db.get(Signup, p.a_signup_id)
        if signup:
            result.append({
                "player_id": signup.player_id,
                "player_name": signup.player_name,
                "signup_id": signup.id,
            })
    return result