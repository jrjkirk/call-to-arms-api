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
from auth import require_user

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

    # Block drops once pairings are published for this week/system
    gate = db.exec(
        select(PublishState)
        .where(PublishState.week == week)
        .where(PublishState.system == system)
    ).first()
    if gate and gate.published:
        raise HTTPException(
            status_code=409,
            detail="Pairings have been published — contact the session organiser if you need to drop out.",
        )

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