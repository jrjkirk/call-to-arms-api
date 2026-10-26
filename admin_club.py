"""Admin endpoints a club runs for itself: table-booking emails, Discord
webhooks, schedules, the mission pool, the landing page and its carousel and
events, and the Discord membership gate. Split out of admin.py; shared
helpers still live there.
"""
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session, select

from auth import (
    DISCORD_CLIENT_ID,
    admin_scopes,
    get_session,
    require_admin,
    require_super_admin,
)
import discord_guild
from database import scoped, log_audit
from league import (
    _current_season_id,
    _season_champion,
)
from models import Club, ClubEvent, ClubSystem, ClubWebhook, LeagueSeason, Mission, SystemConfig, TableBookingConfig, TableBookingNotification, User
import storage
from system_overrides import OVERRIDABLE_FIELDS as OVERRIDABLE_SYSTEM_FIELDS
from table_booking import compute_table_booking, render_table_booking_email, send_table_booking_notification
from signups import (
    _discord_gate_mode,
    _get_system_config,
)
import vibes

from admin import (
    _apply_club_profile_update,
    _club_profile_dict,
    _delete_club_logo_for,
    _require_system_scope,
    _upload_club_logo_for,
    _validate_schedule_fields,
    ClubProfileBody,
    MAX_CLUB_IMAGE_BYTES,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_require_any_admin = require_admin


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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
WEBHOOK_TYPES_PER_SYSTEM: tuple[str, ...] = (
    "signup", "pairings", "call_to_arms", "level_up", "call_outs",
)
# Shown (and creatable) only for systems that ALSO have their league enabled
# (ClubSystem.league_enabled) — these were club-wide (system_id always NULL)
# before leagues themselves became per-system; a club running two leagues
# couldn't route their result/rankings/achievement posts to different
# channels. Migrated existing club-level rows to their club's single
# league-enabled system — see migrations/split_league_webhooks_per_system.py.
WEBHOOK_TYPES_LEAGUE: tuple[str, ...] = ("league_result", "achievement", "league_rankings")
# Club-level, system_id always NULL. Venue bookings aren't a game system — the
# bar has one staff channel, not one per game night — so this deliberately does
# NOT join the per-system grid.
WEBHOOK_TYPES_CLUB: tuple[str, ...] = ("venue_booking",)
ALL_WEBHOOK_TYPES: frozenset[str] = frozenset(
    WEBHOOK_TYPES_PER_SYSTEM + WEBHOOK_TYPES_LEAGUE + WEBHOOK_TYPES_CLUB
)


def _mask_webhook_row(row: Optional[ClubWebhook]) -> dict:
    """Never return the full URL — last 4 characters only, so an operator can
    sanity-check "is this the webhook I think it is" without re-exposing the
    secret. Encryption-at-rest is a separate, deferred hardening step; this
    masking is the actual security control for this slice."""
    if row is None or not row.url:
        return {"configured": False}
    return {"configured": True, "last_four": "..." + row.url[-4:]}


def _require_webhook_scope(db: Session, user: User, system_id: Optional[int]) -> None:
    """A club super-admin may set any of their club's webhooks; a system admin
    may set their own system's.

    Webhooks are per-system across the board — signup/pairings/call_to_arms
    always, and the league ones since leagues went modular — so "which channel
    does my game night post to" is genuinely that system admin's call, and it
    was only ever super-admin-gated because the whole grid lived on a
    club-level page.
    """
    if user.is_platform_admin or user.is_super_admin:
        return
    if system_id is None:
        raise HTTPException(
            status_code=403, detail="Only a club super-admin can set club-wide webhooks."
        )
    system = db.get(SystemConfig, system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")
    _require_system_scope(system.legacy_system_name, user, db)


@router.get("/webhooks")
def list_club_webhooks(
    user: User = Depends(_require_any_admin),
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
                # The scope key the per-system Discord tab filters this grid by.
                "legacy_system_name": system.legacy_system_name,
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
                "legacy_system_name": system.legacy_system_name,
                **_mask_webhook_row(row),
            })

    # A system admin sees only their own systems' rows. The masked value is
    # harmless, but the per-system Discord panel renders this grid directly and
    # listing rows they'd be refused on save is a worse answer than not
    # offering them.
    if not (user.is_platform_admin or user.is_super_admin):
        scopes = admin_scopes(user, db, user.club_id)
        by_id = {s.id: s.legacy_system_name for s in systems}
        grid = [g for g in grid if by_id.get(g["system_id"]) in scopes]
    return grid


class ClubWebhookBody(BaseModel):
    webhook_type: str
    system_id: Optional[int] = None
    url: str


@router.post("/webhooks")
def upsert_club_webhook(
    body: ClubWebhookBody,
    user: User = Depends(_require_any_admin),
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

    _require_webhook_scope(db, user, body.system_id)

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
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Delete the caller's own club's webhook row if it exists. Idempotent."""
    _require_webhook_scope(db, user, system_id)
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
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """List the caller's own club's ClubSystem rows, joined with
    SystemConfig for display.

    A club super-admin sees every system. A system admin sees only the ones
    they administer — the per-system config panel reads this to populate its
    schedule/vibe form, and showing them rows they can't edit would just be
    noise they'd have to work out the rules for.
    """
    rows = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == user.club_id)
    ).all()
    if not (user.is_platform_admin or user.is_super_admin):
        scopes = admin_scopes(user, db, user.club_id)
        rows = [(cs, sc) for cs, sc in rows if sc.legacy_system_name in scopes]
    return [
        {
            "system_id": cs.system_id,
            "system_name": sc.name,
            # The scope string admin UIs address a system by (admin_scopes,
            # Signup.system, …). name and legacy_system_name coincide for every
            # system today, so matching on the display name works right up
            # until one is renamed — hand back the real key.
            "legacy_system_name": sc.legacy_system_name,
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
            # Per-club overrides of the signup form. Each is null when this
            # club has no opinion, paired with a catalogue_* value so the form
            # can show what "inherited" actually resolves to rather than an
            # empty box that means something.
            **{f: getattr(cs, f) for f in OVERRIDABLE_SYSTEM_FIELDS},
            "catalogue": {f: getattr(sc, f) for f in OVERRIDABLE_SYSTEM_FIELDS},
        }
        for cs, sc in rows
    ]


# OVERRIDABLE_SYSTEM_FIELDS is system_overrides.OVERRIDABLE_FIELDS, imported
# rather than restated so the API and the resolver cannot drift: a field the
# resolver honours but the API refuses to set is invisible, and one the API
# accepts but the resolver ignores is a lie.
_OVERRIDE_BOOLS = {"uses_points", "uses_scenarios", "allows_demo",
                   "uses_standby"}


def _clean_system_overrides(raw: dict, catalogue: SystemConfig, existing=None) -> dict:
    """Validate a club's overrides into the columns to write.

    A key that is present with a null value CLEARS that override, which is how
    the UI puts a field back to "inherit the catalogue". A key that is absent
    is left alone. Both matter: without the first there is no way back to the
    platform default once a club has set anything.
    """
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="Overrides must be an object.")
    unknown = [k for k in raw if k not in OVERRIDABLE_SYSTEM_FIELDS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"These cannot be set per club: {sorted(unknown)}.",
        )

    out: dict = {}
    for field, value in raw.items():
        if value is None:
            out[field] = None
            continue
        if field in _OVERRIDE_BOOLS:
            out[field] = bool(value)
        elif field in ("default_points", "max_points"):
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{field} must be a whole number.")
            if n < 0:
                raise HTTPException(status_code=422, detail=f"{field} cannot be negative.")
            if n > 100000:
                raise HTTPException(status_code=422, detail=f"{field} is unrealistically large.")
            out[field] = n
        elif field == "scenario_options":
            if not isinstance(value, list):
                raise HTTPException(status_code=422, detail="scenario_options must be a list.")
            names, seen = [], set()
            for item in value:
                name = str(item).strip()
                if not name or name.lower() in seen:
                    continue
                if len(name) > 60:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Scenario names must be 60 characters or fewer: {name!r}.",
                    )
                seen.add(name.lower())
                names.append(name)
            # An empty list is how the UI clears the override, so store NULL
            # rather than [] and keep one representation of "unset".
            out[field] = names or None
        elif field == "default_scenario":
            out[field] = str(value).strip() or None
        else:  # pragma: no cover - guarded by the unknown-key check above
            raise HTTPException(status_code=422, detail=f"Unhandled field {field}.")

    # Cross-field checks, run against the values that will actually be in
    # force: an override merged over the catalogue, not the override alone. A
    # club that sets only max_points still has to end up above the default it
    # inherited.
    def resolved(field):
        """What this field will be after the write: the value being set, else
        the club's existing override, else the catalogue."""
        if field in out:
            if out[field] is not None:
                return out[field]
            return getattr(catalogue, field)  # explicitly cleared
        current = getattr(existing, field, None) if existing is not None else None
        return current if current is not None else getattr(catalogue, field)

    if resolved("uses_points"):
        default_pts, max_pts = resolved("default_points"), resolved("max_points")
        if max_pts is not None and default_pts is not None and default_pts > max_pts:
            raise HTTPException(
                status_code=422,
                detail=f"Default points ({default_pts}) cannot be above the maximum ({max_pts}).",
            )

    if resolved("uses_scenarios"):
        options = resolved("scenario_options") or []
        if not options:
            raise HTTPException(
                status_code=422,
                detail="Turn scenarios on and there has to be at least one scenario to pick.",
            )
        chosen = resolved("default_scenario")
        if chosen not in options:
            # Only an error when the caller ASKED for this default. An
            # inherited one going stale is the ordinary consequence of writing
            # a new scenario list, and telling someone their save failed
            # because of a value they did not send is not useful — so that
            # case falls to the first scenario instead. Without the
            # distinction, replacing a club's whole scenario list would be
            # impossible in one call.
            if "default_scenario" in out and out["default_scenario"] is not None:
                raise HTTPException(
                    status_code=422,
                    detail=f"The default scenario must be one of {options}.",
                )
            out["default_scenario"] = options[0]

    return out


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
    # Per-club overrides of the signup form. Sent as one object so "the client
    # did not mention overrides at all" (omitted → None) stays distinguishable
    # from "clear this one" (present, null). A flat field could not express
    # both, and clearing an override is the operation that has to work.
    overrides: Optional[dict] = None


@router.post("/club-systems")
def update_club_system_schedule(
    body: ClubSystemScheduleBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Enable/disable/reschedule a catalogue system for the caller's own
    club. Genuine upsert — no longer requires a platform admin to have
    enabled the system for this club first; any club's own super-admin can
    self-service enable or disable any catalogue system. club_id always
    comes from user.club_id, never the request body — same non-negotiable
    rule as scoped() everywhere else. Same shape/validation as the
    platform-admin equivalent, POST /admin/platform/clubs/{club_id}/systems.

    TWO CALLERS, TWO POWERS (2026-08-20). A club super-admin may do anything
    here, including adding a system to the club or switching one off. That
    stays a club-level decision: it changes what the club runs.

    A system's own admin may reschedule and re-vibe THEIR system — it's their
    game night, and making them queue behind a super-admin for a cadence change
    was the point of moving this into the per-system panel. They may NOT flip
    `enabled`, and may not create a ClubSystem row that doesn't exist yet
    (they'd have no scope over a system the club doesn't run).
    """
    system = db.get(SystemConfig, body.system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")

    is_super = user.is_platform_admin or user.is_super_admin
    if not is_super:
        _require_system_scope(system.legacy_system_name, user, db)
        existing_row = db.exec(
            scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == body.system_id)
        ).first()
        if existing_row is None:
            raise HTTPException(
                status_code=403,
                detail="Only a club super-admin can add a system to the club.",
            )
        if bool(body.enabled) != bool(existing_row.enabled):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Only a club super-admin can enable or disable a system. "
                    "You can change how yours runs, but not whether it runs."
                ),
            )

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
        # Names are no longer checked against CANONICAL_VIBES. That list was the
        # point of the restriction and is now the point of the feature: a club
        # names its own vibes, such as The Old World's "Battle March". What IS
        # checked is the behaviour, because the matcher branches on it.
        # Checked against the RAW input, not the parsed specs: vibes.parse falls
        # back to soft for anything it doesn't recognise, which is right when
        # reading stored config written by another version, and wrong here,
        # where a typo should be rejected rather than silently downgraded.
        bad = [
            item.get("name")
            for item in body.vibe_options
            if isinstance(item, dict)
            and str(item.get("behaviour", "") or "").strip().lower() not in vibes.BEHAVIOURS
        ]
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid vibe behaviour on: {bad}. "
                    f"Must be one of {list(vibes.BEHAVIOURS)}."
                ),
            )
        specs = vibes.parse(body.vibe_options)
        long = [v.name for v in specs if len(v.name) > 40]
        if long:
            raise HTTPException(
                status_code=422,
                detail=f"Vibe names must be 40 characters or fewer: {long}.",
            )
        names = vibes.names(specs)
        vibe_options = vibes.to_storage(specs) or None
        default_vibe = None
        if names:
            default_vibe = body.default_vibe if body.default_vibe in names else names[0]
        vibe_fields = {"vibe_options": vibe_options, "default_vibe": default_vibe}

    existing = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == user.club_id,
            ClubSystem.system_id == body.system_id,
        )
    ).first()

    # Signup-form overrides. Omitted leaves every one unchanged; a key present
    # with a null value clears that one back to the catalogue. `existing` goes
    # in because a partial update has to be checked against what will actually
    # be in force: a club sending only scenario_options must still be told if
    # the result leaves the scenarios it turned on last week with nothing to
    # pick from.
    override_fields = (
        _clean_system_overrides(body.overrides, system, existing)
        if body.overrides is not None else {}
    )

    fields = dict(
        enabled=body.enabled,
        session_day=body.session_day,
        session_cadence=body.session_cadence,
        cadence_anchor=body.cadence_anchor,
        session_start_time=body.session_start_time,
        **vibe_fields,
        **override_fields,
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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




class ClubSystemCarouselBody(BaseModel):
    system: str
    blurb: Optional[str] = None
    accent_color: Optional[str] = None


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# A club night's blurb sits on its card on the Club page, which is only so
# big. 250 fits the longest one in use (The Old World's, 211) with room.
CAROUSEL_BLURB_MAX = 250


@router.post("/club-systems/carousel")
def update_carousel_card(
    body: ClubSystemCarouselBody,
    user: User = Depends(_require_any_admin),
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

    if body.blurb and len(body.blurb.strip()) > CAROUSEL_BLURB_MAX:
        raise HTTPException(status_code=422, detail=f"Keep the blurb to {CAROUSEL_BLURB_MAX} characters.")

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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
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
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    ev = _event_or_404(event_id, user, db)
    db.delete(ev)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Discord membership gate — per-club configuration (Slab 2)
#
# Setup is deliberately split into two halves that different people may do,
# at different times: choosing the SERVER (any app admin can, and it is
# usually auto-derived from the club's existing invite link) and ADDING THE
# BOT (needs Manage Server on the Discord, which at some clubs belongs to
# someone outside the app's admin team entirely). Neither blocks the other,
# and the status this endpoint reports has to make it obvious which half is
# outstanding — otherwise an admin is left guessing whether the person they
# messaged has done their bit.
# ---------------------------------------------------------------------------

class DiscordGateBody(BaseModel):
    guild_id: Optional[str] = None
    mode: Optional[str] = None




def _connected_guild_name(guild_id: Optional[str], guilds: Optional[list]) -> Optional[str]:
    """The guild's name if the bot is in it, else None. Read from the list we
    already fetched rather than a second lookup."""
    if not guild_id or guilds is None:
        return None
    for g in guilds:
        if g.get("id") == guild_id:
            return g.get("name")
    return None


def _discord_gate_state(guild_id: Optional[str], club_discord_url: Optional[str], mode: str) -> dict:
    """Everything the admin panel needs to render the gate section.

    Takes plain values, not a live Club/Session, because callers MUST release
    their database connection before calling this — it makes external HTTP
    calls to Discord, and this endpoint is loaded as part of the admin page's
    ~40-request parallel burst. Holding a Supabase pooler connection open
    across that latency is what turns a burst into dropped connections.

    One Discord call in the normal case: `bot_guilds()` answers "is the bot
    configured", "which servers is it in", and "what is ours called" at once.
    (It pages at 200 guilds — far beyond this platform's scale, and the club
    picker would be unusable long before that anyway.)
    """
    guilds = discord_guild.bot_guilds()
    bot_configured = guilds is not None

    name = _connected_guild_name(guild_id, guilds)
    connected = name is not None

    # Only offer a suggestion when nothing is set yet — never nag an admin who
    # has deliberately pointed the gate at a different server than the invite.
    # Costs a second call, but only in the not-yet-configured state.
    suggested = None
    if not guild_id and bot_configured:
        suggested = discord_guild.resolve_invite_guild_id(club_discord_url)

    return {
        "bot_configured": bot_configured,
        "bot_username": None,
        "bot_invite_url": (
            discord_guild.bot_invite_url(DISCORD_CLIENT_ID) if DISCORD_CLIENT_ID else None
        ),
        "guild_id": guild_id,
        "guild_name": name,
        "connected": connected,
        "mode": mode,
        # Enforcement is refused unless the check can actually run. A club
        # sitting on "enforce" with no working connection would fail open on
        # every signup while the UI claimed the gate was on — false
        # reassurance is worse than being plainly off.
        "can_enforce": connected,
        "suggested_guild_id": suggested,
        "club_discord_url": club_discord_url,
        "available_guilds": guilds,
    }


class ClubSystemDiscordGateBody(BaseModel):
    system: str
    # Every field below is omit-to-leave-unchanged (model_fields_set), so the
    # panel can save one control at a time without echoing the rest back.
    enabled: Optional[bool] = None
    mode: Optional[str] = None
    guild_id: Optional[str] = None
    discord_url: Optional[str] = None


def _club_system_gate_state(
    system: str,
    enabled: bool,
    mode: Optional[str],
    own_guild_id: Optional[str],
    own_discord_url: Optional[str],
    club_name: str,
    club_guild_id: Optional[str],
    club_discord_url: Optional[str],
    force_refresh: bool = False,
) -> dict:
    """Everything the per-system gate panel needs to render.

    Takes plain values rather than a live ClubSystem/Session for the same
    reason as _discord_gate_state above: it makes external HTTP calls to
    Discord, and callers MUST release their pooler connection first.

    Reports the OWN and the EFFECTIVE value of the server separately. A panel
    that only showed the effective one couldn't distinguish "this system has
    its own server" from "this system is borrowing the club's", which is the
    single most important thing for an admin at a club like EGNWGC to see
    before switching enforcement on.
    """
    # Cached by default: the admin page renders one of these panels per system
    # and Discord allows one bot_guilds call per second, so an uncached burst
    # 429s all but the first. force_refresh is the admin's explicit "Check
    # connection" press, which must see through the cache.
    guilds = discord_guild.bot_guilds(force=force_refresh)
    bot_configured = guilds is not None

    effective_guild_id = own_guild_id or club_guild_id
    name = _connected_guild_name(effective_guild_id, guilds)
    connected = name is not None

    return {
        "system": system,
        "enabled": enabled,
        # NULL mode means "opted in, still watching" — surfaced resolved so the
        # panel never has to re-implement the fallback.
        "mode": (mode or "monitor"),
        "guild_id": effective_guild_id,
        "guild_id_own": own_guild_id,
        "inherits_guild": own_guild_id is None and club_guild_id is not None,
        "guild_name": name,
        "connected": connected,
        # Enforcement is refused unless the check can actually run. A system
        # sitting on "enforce" with no working connection would fail open on
        # every signup while the UI claimed the gate was on — false reassurance
        # is worse than being plainly off.
        "can_enforce": connected,
        "discord_url": own_discord_url or club_discord_url,
        "discord_url_own": own_discord_url,
        "inherits_url": own_discord_url is None and club_discord_url is not None,
        "club_name": club_name,
        "club_guild_id": club_guild_id,
        "club_discord_url": club_discord_url,
        "bot_configured": bot_configured,
        "bot_invite_url": (
            discord_guild.bot_invite_url(DISCORD_CLIENT_ID) if DISCORD_CLIENT_ID else None
        ),
        "available_guilds": guilds,
    }


def _load_club_system_for_gate(db: Session, user: User, system: str) -> tuple:
    """Resolve + authorize the (club, system) row a gate call is about."""
    _require_system_scope(system, user, db)
    config = _get_system_config(db, system)
    if config is None:
        raise HTTPException(status_code=422, detail="Unknown system.")
    cs = db.exec(
        scoped(ClubSystem, user.club_id).where(ClubSystem.system_id == config.id)
    ).first()
    if cs is None:
        raise HTTPException(status_code=404, detail="System not enabled for this club.")
    club = db.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")
    return cs, club


@router.get("/club-systems/discord-gate")
def get_club_system_discord_gate(
    system: str,
    refresh: bool = False,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """That system's own admin reads its Discord gate config — same ownership
    model as the carousel card and mission pool above.

    Per system rather than per club because one club's game nights can each
    run out of a different Discord server: at EGNWGC, Kill Team and The Old
    World are separate servers and neither is a club-wide one.
    """
    cs, club = _load_club_system_for_gate(db, user, system)
    state_args = (
        system, cs.discord_gate_enabled, cs.discord_gate_mode,
        cs.discord_guild_id, cs.discord_url,
        club.name, club.discord_guild_id, club.discord_url,
    )
    # Return the connection to the pool before any Discord call. The pool is
    # bounded (database.py), and the admin page loads one of these per system
    # in a parallel burst — holding a connection across external HTTP latency
    # is exactly how a bounded pool gets exhausted.
    db.close()
    return _club_system_gate_state(*state_args, force_refresh=refresh)


@router.post("/club-systems/discord-gate")
def set_club_system_discord_gate(
    body: ClubSystemDiscordGateBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """That system's own admin sets its Discord gate: opt in/out, which
    server, which invite link, and monitor vs enforce."""
    cs, club = _load_club_system_for_gate(db, user, body.system)
    provided = body.model_fields_set

    if "guild_id" in provided:
        raw = (body.guild_id or "").strip()
        if raw == "":
            # Clearing falls the system back to the club's server, which is
            # the right behaviour for a club with one Discord for everything.
            cs.discord_guild_id = None
        else:
            # Accept a pasted invite or channel URL as readily as a raw
            # snowflake — an admin who copies the link out of their browser
            # shouldn't hit a validation error for it.
            resolved = (
                raw if raw.isdigit()
                else (discord_guild.guild_id_from_url(raw)
                      or discord_guild.resolve_invite_guild_id(raw))
            )
            if not resolved or not resolved.isdigit():
                raise HTTPException(
                    status_code=422,
                    detail="Enter a Discord server ID (numbers only), or paste a server invite link.",
                )
            cs.discord_guild_id = resolved

    if "discord_url" in provided:
        cs.discord_url = (body.discord_url or "").strip() or None

    if "mode" in provided and body.mode is not None:
        mode = body.mode.strip().lower()
        if mode not in {"monitor", "enforce"}:
            raise HTTPException(status_code=422, detail="Mode must be monitor or enforce.")
        cs.discord_gate_mode = mode

    if "enabled" in provided and body.enabled is not None:
        cs.discord_gate_enabled = bool(body.enabled)

    # One combined check rather than one per field: enabled, mode and guild can
    # all arrive in the same save, and what matters is the state they land in.
    if cs.discord_gate_enabled and (cs.discord_gate_mode or "monitor") == "enforce":
        effective_guild = cs.discord_guild_id or club.discord_guild_id
        if not effective_guild:
            raise HTTPException(
                status_code=422,
                detail="Set this system's Discord server before enforcing the gate.",
            )
        if _connected_guild_name(effective_guild, discord_guild.bot_guilds()) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The bot can't see that Discord server yet. It needs to be added "
                    "by someone with Manage Server. Send them the invite link, then try again."
                ),
            )

    db.add(cs)
    # log_audit does not commit — it must land in the same transaction as the
    # change it records, so it goes before the commit, not after.
    log_audit(
        db, user, "club_system_discord_gate", target_type="club_system",
        target_id=cs.id,
        detail=(
            f"system={body.system} enabled={cs.discord_gate_enabled} "
            f"mode={cs.discord_gate_mode or 'monitor'} guild={cs.discord_guild_id or '(inherits)'}"
        ),
    )
    db.commit()
    db.refresh(cs)

    state_args = (
        body.system, cs.discord_gate_enabled, cs.discord_gate_mode,
        cs.discord_guild_id, cs.discord_url,
        club.name, club.discord_guild_id, club.discord_url,
    )
    db.close()  # release before the Discord calls, same as the GET path
    return _club_system_gate_state(*state_args)


@router.get("/discord-gate")
def get_discord_gate(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")
    guild_id, club_discord_url = club.discord_guild_id, club.discord_url
    mode = _discord_gate_mode(db, club.id)
    # Return the connection to the pool before any Discord call — holding one
    # across external HTTP latency is how a bounded pool gets exhausted.
    db.close()
    return _discord_gate_state(guild_id, club_discord_url, mode)


@router.post("/discord-gate")
def set_discord_gate(
    body: DiscordGateBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    club = db.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")

    if body.guild_id is not None:
        raw = body.guild_id.strip()
        if raw == "":
            club.discord_guild_id = None
        else:
            # Accept a pasted invite or channel URL as readily as a raw
            # snowflake — an admin who copies the link out of their browser
            # shouldn't hit a validation error for it.
            resolved = (
                raw if raw.isdigit()
                else (discord_guild.guild_id_from_url(raw)
                      or discord_guild.resolve_invite_guild_id(raw))
            )
            if not resolved or not resolved.isdigit():
                raise HTTPException(
                    status_code=422,
                    detail="Enter a Discord server ID (numbers only), or paste a server invite link.",
                )
            club.discord_guild_id = resolved
        db.add(club)
        db.commit()
        db.refresh(club)

    if body.mode is not None:
        # Refused outright, rather than accepted and quietly ignored. Every
        # gated action now resolves through the PER-SYSTEM opt-in
        # (ClubSystem.discord_gate_enabled), so a club-level mode can no longer
        # switch anything on — and a club sitting on "enforce" while nothing is
        # actually checked is precisely the false reassurance can_enforce exists
        # to prevent. The club_settings key is left readable for the
        # system=None fallback in signups.resolve_discord_gate, but nothing can
        # set it any more.
        raise HTTPException(
            status_code=422,
            detail=(
                "The membership gate is set per game night, not per club. Open that "
                "system's admin section and switch it on there."
            ),
        )

    guild_id, club_discord_url = club.discord_guild_id, club.discord_url
    mode_now = _discord_gate_mode(db, club.id)
    db.close()  # release before the Discord calls, same as the GET path
    return _discord_gate_state(guild_id, club_discord_url, mode_now)
