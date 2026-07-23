"""Read-only admin analytics — powers the "Command Table" dashboard.

Every endpoint is club-scoped via the caller's own user.club_id (never a
request param) using scoped(Model, club_id), and per-system endpoints are
gated by the same scope check the rest of admin.py uses. These are pure
aggregate reads — no writes, so no WRITE_ALLOWED_TABLES changes.

Date gotcha: Signup.week / Pairing.week / LeagueResult.result_date are
"DD/MM/YYYY" *strings*, so any time-ordering parses to real dates first
(_parse_week) rather than sorting lexically.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from auth import require_user, admin_scopes, valid_scopes
from database import get_session, scoped
from models import (
    ClubSystem,
    LeagueRating,
    Pairing,
    PublishState,
    Signup,
    SystemConfig,
    User,
)
from week_logic import next_session_date

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


def _require_system_scope(system: str, user: User, db: Session) -> None:
    """Mirror of admin.py's gate, duplicated here to keep analytics.py from
    importing the heavy admin module."""
    if system not in valid_scopes(db):
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if system not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{system}' required.")


def _parse_week(w: str) -> Optional[date]:
    try:
        return datetime.strptime(w, "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


def _dedupe_signups(rows: list[Signup]) -> list[Signup]:
    """Latest signup per player (rows must be newest-first). Anonymous rows
    (no player_id) each count once."""
    latest: dict = {}
    for s in rows:
        key = s.player_id if s.player_id is not None else id(s)
        if key not in latest:
            latest[key] = s
    return list(latest.values())


def _system_config(db: Session, system: str) -> Optional[SystemConfig]:
    return db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()


@router.get("/overview")
def analytics_overview(
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Per-system "situation" for each system the caller administers: this
    system's next session date, how many players are signed up for it, and
    whether pairings for that session are published / drafted / not yet made.
    One call powers the whole status-tile row."""
    scopes = sorted(admin_scopes(user, db))
    today = date.today()
    out = []
    for system in scopes:
        config = _system_config(db, system)
        if config is None:
            continue
        cs = db.exec(
            select(ClubSystem).where(
                ClubSystem.club_id == user.club_id,
                ClubSystem.system_id == config.id,
            )
        ).first()
        if cs is None:
            continue

        target = next_session_date(
            cs.session_day, cs.session_cadence, cs.cadence_anchor, today
        )
        week = target.strftime("%d/%m/%Y")

        signup_rows = db.exec(
            scoped(Signup, user.club_id)
            .where(Signup.system == system)
            .where(Signup.week == week)
            .order_by(Signup.created_at.desc())
        ).all()
        signups = len(_dedupe_signups(signup_rows))

        pairing_count = len(db.exec(
            scoped(Pairing, user.club_id)
            .where(Pairing.system == system)
            .where(Pairing.week == week)
        ).all())
        pub = db.exec(
            scoped(PublishState, user.club_id)
            .where(PublishState.system == system)
            .where(PublishState.week == week)
        ).first()
        published = bool(pub and pub.published)

        # status: live (published) > drafted (pairings exist, unpublished) > none
        if published:
            status = "live"
        elif pairing_count > 0:
            status = "drafted"
        else:
            status = "none"

        out.append({
            "system": system,
            "slug": config.slug,
            "name": config.name,
            "next_session": week,
            "session_day": cs.session_day,
            "signups": signups,
            "pairing_count": pairing_count,
            "status": status,
        })

    out.sort(key=lambda r: r["name"])
    return {"generated_at": datetime.utcnow().isoformat(), "systems": out}


@router.get("/signups-over-time")
def signups_over_time(
    system: str,
    weeks: int = 16,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """De-duped signup headcount per session week for one system, oldest to
    newest, capped to the most recent `weeks` sessions."""
    _require_system_scope(system, user, db)
    rows = db.exec(
        scoped(Signup, user.club_id)
        .where(Signup.system == system)
        .order_by(Signup.created_at.desc())
    ).all()

    by_week: dict[str, list[Signup]] = {}
    for s in rows:
        by_week.setdefault(s.week, []).append(s)

    series = []
    for wk, wk_rows in by_week.items():
        d = _parse_week(wk)
        if d is None:
            continue
        series.append({"week": wk, "date": d.isoformat(), "count": len(_dedupe_signups(wk_rows))})

    series.sort(key=lambda r: r["date"])
    if weeks > 0:
        series = series[-weeks:]
    return series


@router.get("/games-over-time")
def games_over_time(
    system: str,
    weeks: int = 16,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Number of real games (pairings with two players; BYEs excluded) run
    per session week for one system, oldest to newest."""
    _require_system_scope(system, user, db)
    rows = db.exec(
        scoped(Pairing, user.club_id)
        .where(Pairing.system == system)
    ).all()

    by_week: dict[str, int] = {}
    for p in rows:
        if p.b_signup_id is None:
            continue  # BYE, not a game
        by_week[p.week] = by_week.get(p.week, 0) + 1

    series = []
    for wk, count in by_week.items():
        d = _parse_week(wk)
        if d is None:
            continue
        series.append({"week": wk, "date": d.isoformat(), "count": count})

    series.sort(key=lambda r: r["date"])
    if weeks > 0:
        series = series[-weeks:]
    return series


@router.get("/faction-popularity")
def faction_popularity(
    system: str,
    top: int = 12,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """How often each faction has been fielded for one system, counting one
    entry per (player, week) signup so a regular isn't counted many times in
    a single night. Sorted most-played first, capped to `top`."""
    _require_system_scope(system, user, db)
    rows = db.exec(
        scoped(Signup, user.club_id)
        .where(Signup.system == system)
        .order_by(Signup.created_at.desc())
    ).all()

    # De-dupe per (week, player) so multiple edits of one signup count once.
    seen: set = set()
    counts: dict[str, int] = {}
    for s in rows:
        key = (s.week, s.player_id if s.player_id is not None else id(s))
        if key in seen:
            continue
        seen.add(key)
        faction = (s.faction or "").strip()
        if not faction:
            continue
        counts[faction] = counts.get(faction, 0) + 1

    series = [{"faction": f, "count": c} for f, c in counts.items()]
    series.sort(key=lambda r: (-r["count"], r["faction"]))
    return series[:top] if top > 0 else series


@router.get("/rating-distribution")
def rating_distribution(
    system: str,
    bucket: int = 50,
    user: User = Depends(require_user),
    db: Session = Depends(get_session),
):
    """Histogram of current league ratings for one system (all seasons'
    latest ratings for that system), bucketed by `bucket` rating points.
    Shows how tightly bunched or spread the club's skill is."""
    _require_system_scope(system, user, db)
    config = _system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")

    ratings = [
        r.rating for r in db.exec(
            scoped(LeagueRating, user.club_id)
            .where(LeagueRating.system_id == config.id)
        ).all()
        if r.rating is not None
    ]
    if not ratings:
        return {"buckets": [], "count": 0}

    if bucket <= 0:
        bucket = 50
    lo = int(min(ratings) // bucket) * bucket
    hi = int(max(ratings) // bucket) * bucket
    buckets = []
    edge = lo
    while edge <= hi:
        count = sum(1 for r in ratings if edge <= r < edge + bucket)
        buckets.append({"min": edge, "max": edge + bucket, "count": count})
        edge += bucket

    return {"buckets": buckets, "count": len(ratings)}
