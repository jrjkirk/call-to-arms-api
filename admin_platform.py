"""Platform-admin endpoints (cross-club): the game-system catalogue, clubs and
club requests, other clubs' profiles, onboarding emails, the site banner, job
health, the audit log and user lookup. Split out of admin.py; shared helpers
still live there.
"""
import os
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, or_, select

from auth import (
    get_session,
    require_admin,
    require_platform_admin,
)
from database import scoped, log_audit, club_app_url
from identity import DISCORD, ProviderProfile, create_user_for_profile, display_name_for, find_user_for_profile
from models import AdminRole, AppSetting, AuditLogEntry, Club, ClubRequest, ClubSystem, ClubWebhook, Player, PlatformBanner, PublishState, ScheduledJobRun, Signup, SystemConfig, UK_REGIONS, User
import storage
from observability import capture
from systems import (
    FACTION_ORDERS,
    ordered_factions,
    resolved_faction_groups,
    resolved_factions,
    resolved_icon_folder,
)
import club_emails

from admin import (
    _apply_club_profile_update,
    _club_profile_dict,
    _COMMUNITY_DISCORD_KEY,
    _community_discord_url,
    _delete_club_logo_for,
    _upload_club_logo_for,
    _validate_schedule_fields,
    ClubProfileBody,
)

router = APIRouter(prefix="/admin", tags=["admin"])

_require_any_admin = require_admin


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
    # A system's ruleset is authored here. It used to live in systems/<name>.py
    # and need a deploy; see systems/__init__.py.
    faction_list: Optional[list[str]] = None
    faction_groups: Optional[list[dict]] = None
    icon_folder: Optional[str] = None
    # A new system sorts alphabetically unless told otherwise: most lists are
    # armies with no order of their own.
    faction_order: str = "alphabetical"
    active: bool = True


class SystemConfigEditBody(BaseModel):
    """Same shape as SystemConfigCreateBody minus slug — slug is immutable
    after creation, since it names the logo and icon files on disk."""
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
    faction_list: Optional[list[str]] = None
    faction_groups: Optional[list[dict]] = None
    icon_folder: Optional[str] = None
    # None keeps the system's current order, so a client that predates the
    # setting can't reset it by saving.
    faction_order: Optional[str] = None
    active: bool = True


def _clean_ruleset(
    faction_list: Optional[list[str]],
    faction_groups: Optional[list[dict]],
    icon_folder: Optional[str],
    order: str = "custom",
) -> tuple[Optional[list[str]], Optional[list[dict]], Optional[str]]:
    """Normalise an authored ruleset, or return NULLs meaning "no factions yet".

    An empty faction list stores NULL rather than [], so there is one way to
    say "none", not two. (NULL used to mean "fall back to the code module";
    those were deleted 2026-10-21.)

    Groups are the authored grouping and are rebuilt from the flat list rather
    than trusted: a faction named in a group but missing from the list would
    appear in the dropdown and then fail validation on submit.
    """
    factions = [f.strip() for f in (faction_list or []) if f and f.strip()]
    # Order-preserving de-dupe. Two identical options in a dropdown is a bug
    # report, and the matcher would treat them as one anyway.
    seen: dict[str, None] = {}
    for f in factions:
        seen.setdefault(f, None)
    factions = list(seen)

    if not factions:
        return None, None, (icon_folder or "").strip() or None

    groups_out: list[dict] = []
    for g in faction_groups or []:
        if not isinstance(g, dict):
            continue
        label = str(g.get("label", "")).strip()
        members = [
            str(f).strip() for f in (g.get("factions") or [])
            if str(f).strip() in seen
        ]
        if label and members:
            groups_out.append({"label": label, "factions": members})

    # A group set that does not cover the whole list would silently hide
    # factions from the dropdown, so it is all or nothing.
    grouped = {f for g in groups_out for f in g["factions"]}
    if groups_out and grouped != set(factions):
        missing = sorted(set(factions) - grouped)
        raise HTTPException(
            status_code=422,
            detail=(
                "Every faction must belong to a category once categories are used. "
                f"Not in any category: {', '.join(missing[:5])}"
                + ("…" if len(missing) > 5 else "")
            ),
        )

    factions, groups_out = ordered_factions(factions, groups_out or None, order)
    return factions, groups_out, (icon_folder or "").strip() or None


def _faction_order(value: Optional[str], current: str = "custom") -> str:
    if value is None:
        return current
    if value not in FACTION_ORDERS:
        raise HTTPException(status_code=422, detail="faction_order must be alphabetical or custom.")
    return value


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

Each row carries BOTH its authored ruleset (the raw columns, which the edit
    form binds to) and the resolved one actually in force, plus `ruleset_source`
    so the panel can say whether a system has a faction list yet. ("code" is
    gone: it meant a fallback to a module in systems/, deleted 2026-10-21.)"""
    rows = db.exec(select(SystemConfig).order_by(SystemConfig.name)).all()
    result = []
    for r in rows:
        row = r.model_dump()
        authored = bool(r.faction_list)
        row["ruleset_source"] = "authored" if authored else "none"
        # What is actually served to players right now, whichever source won.
        row["resolved_faction_list"] = resolved_factions(r)
        row["resolved_faction_groups"] = resolved_faction_groups(r)
        row["resolved_icon_folder"] = resolved_icon_folder(r)
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

    data = body.model_dump()
    data["faction_order"] = _faction_order(body.faction_order)
    data["faction_list"], data["faction_groups"], data["icon_folder"] = _clean_ruleset(
        body.faction_list, body.faction_groups, body.icon_folder, data["faction_order"]
    )
    system = SystemConfig(**data)
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

    data = body.model_dump()
    data["faction_order"] = _faction_order(body.faction_order, system.faction_order or "custom")
    data["faction_list"], data["faction_groups"], data["icon_folder"] = _clean_ruleset(
        body.faction_list, body.faction_groups, body.icon_folder, data["faction_order"]
    )
    # logo_path / logo_url are not in the body and must not be touched here:
    # the logo is owned by its own upload endpoint, and a full-replace edit
    # that included them would wipe the artwork every time someone renamed a
    # system.
    for k, v in data.items():
        setattr(system, k, v)
    db.add(system)
    log_audit(db, user, "system.update", "system", system.id, system.name)
    db.commit()
    db.refresh(system)
    return system


def _faction_icon_slug(name: str) -> str:
    """The filename a faction's icon must have.

    Must stay identical to scripts/render_pairings_image.py::_faction_slug and
    the frontend's factionSlug() in src/lib/factions.ts. Three copies of one
    rule is two too many, but the other two are in different languages and one
    of them runs on a GitHub runner — so the rule is duplicated deliberately
    and this comment is the tripwire. Change one, change all three.
    """
    s = (name or "").lower().strip()
    s = s.replace("&", "and")
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return "_".join(s.split())


@router.get("/platform/systems/{system_id}/icon-checklist")
def system_icon_checklist(
    system_id: int,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Exactly which icon files this system needs, and where they go.

    Faction icons stay committed rather than uploaded, and not for want of
    somewhere to put them: the weekly pairings image is rendered by a GitHub
    Actions runner that checks this repo out at main (matplotlib is not in the
    API image — see github_dispatch.py), so the PNGs have to be in git for the
    post to have artwork. Uploading them would mean teaching that renderer to
    fetch and cache remote files, and a slow fetch would degrade the one post a
    club actually looks at.

    So the compromise is: the app cannot place the files, but it can remove
    every chance to get them wrong. This returns the exact slugified names,
    both destinations, and which are already present on this machine.
    """
    system = db.get(SystemConfig, system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")

    folder = resolved_icon_folder(system) or (system.slug or "").upper()
    factions = resolved_factions(system) or []

    repo_root = os.path.dirname(os.path.abspath(__file__))
    icons_dir = os.path.join(repo_root, "icons", folder)

    items = []
    for f in factions:
        slug = _faction_icon_slug(f)
        items.append({
            "faction": f,
            "slug": slug,
            "png": f"{slug}.png",
            "svg": f"{slug}.svg",
            # Only the API side can be checked from here; the web repo is a
            # different deployment and its files are not on this machine.
            "png_present_in_api_repo": os.path.exists(
                os.path.join(icons_dir, f"{slug}.png")
            ),
        })

    return {
        "system": system.name,
        "icon_folder": folder,
        "destinations": {
            # The renderer reads PNGs off disk; matplotlib will not take an SVG.
            "api_png": f"call-to-arms-api/icons/{folder}/",
            # The browser prefers the SVG and falls back to the PNG.
            "web": f"call-to-arms-web/static/icons/{folder}/",
        },
        "factions": items,
        "missing_png_count": sum(1 for i in items if not i["png_present_in_api_repo"]),
        "note": (
            "A faction with no icon still pairs and still renders; it just has "
            "no artwork beside its name."
        ),
    }


# A wordmark is a few tens of KB. 3 MB is generous headroom and still stops an
# accidental camera-roll drop reaching storage.
MAX_SYSTEM_LOGO_BYTES = 3 * 1024 * 1024

SYSTEM_LOGO_GUIDELINES = {
    "formats": ["PNG", "WEBP"],
    "max_size_mb": 3,
    # The browser resizes to this before uploading, so it is a ceiling rather
    # than a requirement. The existing committed logos run 600x157 to 1384x320
    # and every slot renders them with object-fit: contain, so the width is
    # what matters and the height just has to not be silly.
    "max_width": 1200,
    "max_height": 300,
    "recommended": (
        "A transparent PNG wordmark, landscape, around 3:1 to 4:1. It is "
        "scaled down to fit 1200x300 before upload and never scaled up."
    ),
}


@router.post("/platform/systems/{system_id}/logo")
def upload_system_logo(
    system_id: int,
    image: UploadFile = File(...),
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Replace a system's logo.

    Uploaded rather than committed, so adding a system no longer needs a deploy
    to have artwork. The six committed logos in the web repo's static/logos are
    untouched and still serve any system with no upload.
    """
    system = db.get(SystemConfig, system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")

    data = image.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="That file is empty.")
    if len(data) > MAX_SYSTEM_LOGO_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"That image is over {MAX_SYSTEM_LOGO_BYTES // (1024 * 1024)} MB.",
        )
    try:
        storage.extension_for(image.content_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    old_path = system.logo_path
    try:
        object_path, public_url = storage.upload_system_logo(
            data, image.content_type, system_id
        )
    except RuntimeError as e:
        capture(e, kind="system_logo_upload", system_id=system_id)
        raise HTTPException(status_code=502, detail="Could not store the image. Try again.")

    system.logo_path = object_path
    system.logo_url = public_url
    db.add(system)
    log_audit(db, user, "system.logo", "system", system.id, system.name)
    db.commit()
    db.refresh(system)

    # After the commit, and never allowed to fail it: the new logo is live
    # either way, and an orphaned blob is cheaper than losing the row that
    # points at the new one.
    if old_path and old_path != object_path:
        storage.delete_club_image(old_path)
    return {"ok": True, "logo_url": system.logo_url}


@router.delete("/platform/systems/{system_id}/logo")
def delete_system_logo(
    system_id: int,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Drop the uploaded logo. The system falls back to a committed
    /logos/<slug>.png if one exists, which is how all six originals work."""
    system = db.get(SystemConfig, system_id)
    if system is None:
        raise HTTPException(status_code=404, detail="System not found.")
    old_path = system.logo_path
    system.logo_path = None
    system.logo_url = None
    db.add(system)
    log_audit(db, user, "system.logo.delete", "system", system.id, system.name)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)
    return {"ok": True}


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
        "name": display_name_for(u),
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
        log_audit(db, user, "club.super_admin.grant", "user", target.id, f"{display_name_for(target)!r} on club {club.slug!r}")
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
            "name": display_name_for(sa),
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
                "name": display_name_for(u),
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
        log_audit(db, user, "club.super_admin.revoke", "user", target.id, f"{display_name_for(target)!r} on club {club.slug!r}")
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






class CommunityDiscordBody(BaseModel):
    url: Optional[str] = None


@router.get("/platform/community-discord")
def get_community_discord(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    return {"url": _community_discord_url(db)}


@router.post("/platform/community-discord")
def set_community_discord(
    body: CommunityDiscordBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    url = (body.url or "").strip() or None
    if url is not None and not (url.startswith("https://") or url.startswith("http://")):
        raise HTTPException(status_code=422, detail="URL must start with http:// or https://")
    row = db.get(AppSetting, _COMMUNITY_DISCORD_KEY)
    if row is None:
        row = AppSetting(key=_COMMUNITY_DISCORD_KEY, value=url)
    else:
        row.value = url
        row.updated_at = datetime.utcnow()
    db.add(row)
    log_audit(db, user, "community_discord.update", "app_setting", None, f"url={url!r}")
    db.commit()
    return {"url": url}


# ---------------------------------------------------------------------------
# Onboarding email templates (platform admin)
# ---------------------------------------------------------------------------

class ClubEmailTemplateBody(BaseModel):
    kind: str
    # Either may be null to fall back to the built-in default. That is how a
    # template is reset: clear the field rather than hunting for the original
    # wording to paste back.
    subject: Optional[str] = None
    body: Optional[str] = None


class ClubEmailPreviewBody(BaseModel):
    kind: str
    subject: Optional[str] = None
    body: Optional[str] = None


class ClubEmailTestBody(BaseModel):
    kind: str
    to: str
    subject: Optional[str] = None
    body: Optional[str] = None


@router.get("/platform/club-emails")
def list_club_emails(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """The three onboarding emails, as they'll actually go out.

    Returns the default alongside the current wording so the editor can show
    what "reset" would restore without keeping its own copy of the copy.
    """
    out = []
    for kind, spec in club_emails.EMAIL_KINDS.items():
        current = club_emails.get_template(db, kind)
        out.append({
            "kind": kind,
            "label": spec["label"],
            "when": spec["when"],
            "tokens": spec["tokens"],
            "subject": current["subject"],
            "body": current["body"],
            "default_subject": spec["subject"],
            "default_body": spec["body"],
            "customised": current["customised"],
        })
    return out


@router.post("/platform/club-emails")
def save_club_email(
    body: ClubEmailTemplateBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    if body.kind not in club_emails.EMAIL_KINDS:
        raise HTTPException(status_code=422, detail="Unknown email.")

    for part, value in (("subject", body.subject), ("body", body.body)):
        key = f"club_email_{body.kind}_{part}"
        row = db.get(AppSetting, key)
        cleaned = (value or "").strip() or None
        if cleaned is None:
            # Cleared: drop the row entirely so the built-in default takes over
            # again, rather than storing an empty string that renders nothing.
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            row = AppSetting(key=key, value=cleaned)
        else:
            row.value = cleaned
            row.updated_at = datetime.utcnow()
        db.add(row)

    log_audit(db, user, "club_email.update", "app_setting", None, f"kind={body.kind}")
    db.commit()
    return club_emails.get_template(db, body.kind) | {"kind": body.kind}


@router.post("/platform/club-emails/preview")
def preview_club_email(
    body: ClubEmailPreviewBody,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Render unsaved edits against sample data.

    Previewing the draft rather than the stored version is the point: the whole
    reason this endpoint exists is to see a change before a club does.
    """
    if body.kind not in club_emails.EMAIL_KINDS:
        raise HTTPException(status_code=422, detail="Unknown email.")
    spec = club_emails.EMAIL_KINDS[body.kind]
    stored = club_emails.get_template(db, body.kind)
    subject = (body.subject or "").strip() or stored["subject"]
    text = (body.body or "").strip() or stored["body"]
    ctx = club_emails.SAMPLE_CONTEXT
    # The whole email, logo and button and all — a preview of the paragraphs
    # alone tells you nothing about the thing that actually lands.
    return {
        "subject": club_emails.subject_safe(club_emails.fill(subject, ctx)),
        "html": club_emails.preview_html(db, body.kind, subject, text, ctx),
        "text": club_emails.render_text(text, ctx),
        "unknown_tokens": sorted(
            set(re.findall(r"\{([a-z_]+)\}", f"{subject}\n{text}")) - set(spec["tokens"])
        ),
    }


@router.post("/platform/club-emails/test")
def test_send_club_email(
    body: ClubEmailTestBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Send the draft to a real inbox.

    A preview shows the markup; only a real send shows what the client does with
    it. Platform-admin only, and the address is whatever they type — usually
    their own.
    """
    if body.kind not in club_emails.EMAIL_KINDS:
        raise HTTPException(status_code=422, detail="Unknown email.")
    if "@" not in body.to or " " in body.to:
        raise HTTPException(status_code=422, detail="That doesn't look like an email address.")
    stored = club_emails.get_template(db, body.kind)
    subject = (body.subject or "").strip() or stored["subject"]
    text = (body.body or "").strip() or stored["body"]
    ctx = club_emails.SAMPLE_CONTEXT
    outcome = club_emails._send(
        body.to,
        club_emails.subject_safe(club_emails.fill(subject, ctx)),
        club_emails.preview_html(db, body.kind, subject, text, ctx),
        club_emails.render_text(text, ctx),
    )
    log_audit(db, user, "club_email.test_send", "app_setting", None,
              f"kind={body.kind} to={body.to} -> {outcome}")
    db.commit()
    return {"outcome": outcome}


# Every scheduled job that calls record_job_run() — kept as one list so the
# health-check UI can show "never run" for a job with zero rows, not just
# silently omit it.
#
# Must stay in step with scripts/run_*_check.py's JOB_NAME constants. It fell
# behind three of them (call-outs, table-booking, ticket holds), which meant
# the one screen you would open to ask "is a scheduled job broken?" quietly
# answered for two jobs out of five.
KNOWN_SCHEDULED_JOBS = [
    "auto_pairings_check",
    "call_to_arms_check",
    "call_outs_check",
    "table_booking_cutoff_check",
    "ticket_holds_check",
    "league_rankings_check",
]


def _job_run_dict(r: ScheduledJobRun) -> dict:
    return {"job_name": r.job_name, "ran_at": r.ran_at, "status": r.status, "detail": r.detail}


@router.get("/platform/job-runs")
def list_job_runs(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Health-check view of every scheduled job — the most recent run of
    each (so a silently-broken job is visible instead of only surfacing when
    a club complains their pairings never posted), plus a short recent
    history across all of them for spotting flakiness.

    All five run on the in-process scheduler (scheduler.py) every five
    minutes, so a `ran_at` older than that means the scheduler itself is
    stopped, not that a cron was slow. Several also still have a GitHub
    Actions cron as a backstop; those fire about five times a day."""
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


@router.post("/platform/users/{user_id}/reset-name")
def platform_reset_user_name(
    user_id: int,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Clear any account's chosen name, from the cross-club user search."""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    old = target.display_name
    target.display_name = None
    db.add(target)
    log_audit(db, user, "account.name.reset", "user", target.id, repr(old))
    db.commit()
    return {"ok": True, "name": display_name_for(target)}


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
        .where(User.discord_name.ilike(like) | User.display_name.ilike(like))
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
            "user_id": u.id, "discord_name": u.discord_name, "name": display_name_for(u), "display_name": u.display_name,
            "player_name": p.name if p else None,
            "club_id": c.id, "club_name": c.name, "club_slug": c.slug,
            "is_super_admin": u.is_super_admin, "is_platform_admin": u.is_platform_admin,
        })
    for u, c, p in player_rows:
        if u.id in matched_ids:
            continue
        matched_ids.add(u.id)
        result.append({
            "user_id": u.id, "discord_name": u.discord_name, "name": display_name_for(u), "display_name": u.display_name,
            "player_name": p.name,
            "club_id": c.id, "club_name": c.name, "club_slug": c.slug,
            "is_super_admin": u.is_super_admin, "is_platform_admin": u.is_platform_admin,
        })
    return result[:50]


def _club_request_profile(req: ClubRequest) -> Optional[ProviderProfile]:
    """Who asked for this club, as a sign-in identity provisioning can make an
    account for. Falls back to discord_id for a request written before
    identity_provider/identity_subject existed. None for the anonymous
    requests that predate sign-in being required."""
    if req.identity_provider and req.identity_subject:
        provider, subject = req.identity_provider, req.identity_subject
    elif req.discord_id:
        provider, subject = DISCORD, req.discord_id
    else:
        return None
    name = req.discord_name if provider == DISCORD else None
    return ProviderProfile(provider=provider, subject=subject, name=name or req.requester_name)


def _suggest_slug(name: str) -> str:
    """Best-effort hostname-safe slug from a club name, to prefill the provision
    form. The platform admin still confirms/edits it — it becomes the club's
    <slug>.calltoarms.app subdomain, so it must be right before the club goes
    live, but a sensible default saves typing."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "club"


def _possible_duplicates(db: Session, r: ClubRequest) -> list[dict]:
    """Existing clubs that might already be the one being requested.

    Surfaced to the reviewer rather than blocking the requester: two clubs can
    legitimately share a name in different towns, and the person who knows which
    is which is the one reading the request. Without this, the way you find out
    is provisioning a subdomain to the wrong committee member.
    """
    name = (r.club_name or "").strip()
    town = (r.club_location or "").strip()
    if not name and not town:
        return []
    clauses = []
    if name:
        clauses.append(Club.name.ilike(f"%{name}%"))
        # A slug clash is the same club by another spelling more often than not.
        clauses.append(Club.slug == _suggest_slug(name))
    if town:
        clauses.append(Club.address.ilike(f"%{town}%"))
    rows = db.exec(select(Club).where(or_(*clauses)).limit(5)).all()
    return [
        {"id": c.id, "name": c.name, "slug": c.slug, "address": c.address, "active": c.active}
        for c in rows
        if c.id != r.provisioned_club_id
    ]


def _club_request_dict(r: ClubRequest, db: Optional[Session] = None) -> dict:
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
        "provisioned_club_id": r.provisioned_club_id,
        # The requester's own preference wins over our slugified guess — they
        # know what their players will type.
        "suggested_slug": r.preferred_slug or _suggest_slug(r.club_name),
        # Everything the reviewer used to have to ask for by email.
        "discord_name": r.discord_name,
        "discord_id": r.discord_id,
        "region": r.region,
        "preferred_slug": r.preferred_slug,
        "systems": r.systems or [],
        # Per-system nights. Empty on requests predating 2026-09-09, which is
        # why the flat club-wide fields below are still sent: the reviewer
        # needs to see whichever one this request actually carries.
        "system_details": r.system_details or {},
        "club_night_day": r.club_night_day,
        "club_night_time": r.club_night_time,
        "player_count": r.player_count,
        "requester_role": r.requester_role,
        "evidence_url": r.evidence_url,
        # Only computed for the list view, where a reviewer is actually looking.
        "possible_duplicates": _possible_duplicates(db, r) if db is not None else [],
    }


@router.get("/platform/club-requests")
def list_club_requests(
    status: Optional[str] = None,
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """"Please add my club" submissions, newest first.

    ?status=pending|approved|denied filters; omitted returns all. Each row
    carries possible_duplicates, computed here rather than on submit: the
    person who can tell two similarly-named clubs apart is the one reading
    the request, not the one filling in the form."""
    query = select(ClubRequest)
    if status is not None:
        if status not in {"pending", "approved", "denied"}:
            raise HTTPException(status_code=422, detail="status must be one of: pending, approved, denied")
        query = query.where(ClubRequest.status == status)
    rows = db.exec(query.order_by(ClubRequest.created_at.desc())).all()
    return [_club_request_dict(r, db) for r in rows]


def _review_club_request(request_id: int, new_status: str, user: User, db: Session) -> ClubRequest:
    req = db.get(ClubRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Club request not found.")
    req.status = new_status
    req.reviewed_at = datetime.utcnow()
    req.reviewed_by_user_id = user.id
    req.reviewed_by_name = display_name_for(user)
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
    """Marks the request reviewed/approved without creating the Club.

    Kept for the case where a request is judged good but the club isn't being
    set up yet. The normal path is /provision, which approves AND creates in one
    step — and, unlike this, tells the requester. Approving here deliberately
    sends nothing: there is no club yet, so there is nothing to tell them."""
    return _club_request_dict(_review_club_request(request_id, "approved", user, db))


class DenyClubRequestBody(BaseModel):
    # Optional and free text. Most declines are "not yet" or "we couldn't tell
    # you run this club", both of which are worth saying and both of which are
    # fixable — so the email offers a way back either way.
    reason: Optional[str] = None
    notify: bool = True


@router.post("/platform/club-requests/{request_id}/deny")
def deny_club_request(
    request_id: int,
    body: Optional[DenyClubRequestBody] = None,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Decline a request, and by default say so.

    Declines used to be silent: the row changed colour in an admin screen and
    the person who asked waited indefinitely for an answer that already existed.
    `notify: false` is there for spam and obvious junk, which deserves no reply.
    """
    body = body or DenyClubRequestBody()
    req = _review_club_request(request_id, "denied", user, db)

    result = _club_request_dict(req)
    if body.notify:
        outcome = club_emails.send_request_declined(
            db,
            to=req.requester_email,
            requester_name=req.requester_name,
            club_name=req.club_name,
            reason=(body.reason or "").strip() or None,
        )
        if outcome != "sent":
            print(f"[club-request {req.id}] decline email: {outcome}")
        result["email"] = outcome
    return result


class ProvisionClubBody(BaseModel):
    slug: str                       # confirmed/edited by the platform admin
    region: Optional[str] = None    # optional; must be a UK_REGIONS value
    active: bool = True
    # Both default ON: the request now carries a verified Discord identity and
    # the club's own answers, so the normal case is "yes, set it all up". A
    # reviewer who wants to hold something back unticks it deliberately.
    appoint_super_admin: bool = True
    enable_systems: bool = True


@router.post("/platform/club-requests/{request_id}/provision")
def provision_club_request(
    request_id: int,
    body: ProvisionClubBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """One-click onboarding: create the real Club straight from a request and
    mark the request approved + linked, instead of the old flow (approve, then
    hand-create the club via POST /admin/platform/clubs). Name, contact email,
    and address come from the request; the platform admin supplies the slug
    (subdomain) and an optional region.

    Deliberately does NOT enable systems (schedules are club-specific — the new
    admin sets those via the onboarding checklist) and does NOT appoint the
    super-admin (that needs the requester to have logged in via Discord first;
    surfaced as the next onboarding step). Idempotent guard: a request already
    provisioned returns 409."""
    req = db.get(ClubRequest, request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Club request not found.")
    if req.provisioned_club_id is not None:
        raise HTTPException(status_code=409, detail="This request has already been provisioned into a club.")

    slug = body.slug.strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise HTTPException(
            status_code=422,
            detail="Slug must be lowercase letters, digits, and hyphens, with no leading or trailing hyphen.",
        )
    if db.exec(select(Club).where(Club.slug == slug)).first():
        raise HTTPException(status_code=409, detail="A club with this slug already exists.")

    region = body.region.strip() if body.region else None
    if region is not None and region not in UK_REGIONS:
        raise HTTPException(status_code=422, detail="region must be one of the known UK regions.")

    club = Club(
        name=req.club_name,
        slug=slug,
        contact_email=req.requester_email,
        address=req.club_location,
        region=region,
        active=body.active,
    )
    db.add(club)
    db.flush()

    # --- Make the requester this club's super-admin -----------------------
    # This used to be three separate steps with two emails in between: create
    # the club, wait for the requester to sign in and say so, then find them and
    # appoint them. The request now carries their Discord id, so the User row
    # can be created here, already attached to the club that just came into
    # existence. When they next log in, discord_callback finds an existing user
    # and drops them straight into their own admin.
    appointed = None
    requester = _club_request_profile(req)
    if body.appoint_super_admin and requester is not None:
        target = find_user_for_profile(db, requester)
        if target is None:
            target = create_user_for_profile(
                db, requester,
                player_id=None,
                club_id=club.id,
                home_club_id=club.id,
                is_super_admin=True,
            )
        else:
            # An existing player who is starting a club. Their club_id has to
            # move, because club-scoped admin authority is `is_super_admin AND
            # club_id == this club` and there is no multi-club admin model: one
            # person cannot own two clubs at once.
            #
            # Which means moving them can take an owner away from somewhere
            # else. Harmless when that club has another super-admin; when it
            # does not, it silently leaves a live club with nobody who can
            # administer it, discovered weeks later by whoever needs to change
            # something. So the trade gets refused rather than made quietly, and
            # the reviewer decides.
            previous_club_id = target.club_id
            if (
                target.is_super_admin
                and previous_club_id is not None
                and previous_club_id != club.id
            ):
                remaining = db.exec(
                    select(User)
                    .where(User.club_id == previous_club_id)
                    .where(User.is_super_admin == True)
                    .where(User.id != target.id)
                ).all()
                if not remaining:
                    previous = db.get(Club, previous_club_id)
                    previous_name = previous.name if previous else f"club {previous_club_id}"
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"{display_name_for(target)} is the only super-admin of "
                            f"{previous_name}. Making them super-admin here would move "
                            f"them and leave {previous_name} with nobody who can "
                            "administer it. Appoint someone else there first, or "
                            "provision without appointing and sort the admin out "
                            "afterwards."
                        ),
                    )
            target.club_id = club.id
            target.home_club_id = club.id
            target.is_super_admin = True
            db.add(target)
        db.flush()
        appointed = {"user_id": target.id, "discord_name": target.discord_name, "name": display_name_for(target)}
        log_audit(db, user, "club.super_admin.grant", "user", target.id,
                  f"{display_name_for(target)!r} on club {club.slug!r} (from request {req.id})")

    # --- Turn on the systems they asked for --------------------------------
    # Previously skipped, because schedules are club-specific and nobody had
    # asked the club when it meets. The form asks now, so the club arrives with
    # its systems on and its club night set — a starting point the admin can
    # correct, rather than an empty console and a phone call.
    # A club night is required on the form now, so only a request predating that
    # arrives without one. Skipped rather than guessed: session_day is NOT NULL,
    # and inventing "Wednesday" published a specific wrong night on a club's
    # public page and drove every schedule default from it. No systems is
    # obviously unfinished; a plausible wrong answer is not.
    #
    # Each system gets ITS OWN night. The form used to ask one question for the
    # whole club and stamp that answer onto every ClubSystem, always weekly,
    # which was wrong for any club running two games on two nights and put a
    # specific wrong night on their public page.
    #
    # `req.system_details` carries the per-system answers. Requests submitted
    # before 2026-09-09 have NULL there and the flat club-wide columns instead,
    # so those are the fallback and every pending row still provisions.
    #
    # A system with no day is SKIPPED, not guessed. session_day is NOT NULL and
    # inventing "Wednesday" is how a club ended up advertising a night it does
    # not run.
    enabled_systems: list[str] = []
    skipped_systems: list[str] = []
    skipped_systems_reason = None
    details = req.system_details if isinstance(req.system_details, dict) else {}
    fallback_day = (req.club_night_day or "").strip() or None
    fallback_time = (req.club_night_time or "").strip() or None

    if body.enable_systems and req.systems:
        for name in req.systems:
            config = db.exec(
                select(SystemConfig).where(SystemConfig.legacy_system_name == name)
            ).first()
            if config is None:
                continue
            entry = details.get(name) if isinstance(details.get(name), dict) else {}
            day = (entry.get("day") or fallback_day or "").strip() or None
            if not day:
                skipped_systems.append(name)
                continue
            start = (entry.get("time") or fallback_time or "").strip() or None
            cadence = (entry.get("cadence") or "weekly").strip().lower()
            if cadence not in ("weekly", "fortnightly", "monthly"):
                cadence = "weekly"
            db.add(ClubSystem(
                club_id=club.id,
                system_id=config.id,
                enabled=True,
                session_day=day,
                session_cadence=cadence,
                session_start_time=start,
            ))
            enabled_systems.append(name)

        if skipped_systems:
            skipped_systems_reason = (
                f"no club night given for {', '.join(skipped_systems)}, "
                "so nothing was scheduled for them"
            )

    req.status = "approved"
    req.reviewed_at = datetime.utcnow()
    req.reviewed_by_user_id = user.id
    req.reviewed_by_name = display_name_for(user)
    req.provisioned_club_id = club.id
    db.add(req)

    log_audit(db, user, "club.provision", "club", club.id, f"{club.name!r} (slug={club.slug}) from request {req.id}")
    db.commit()
    db.refresh(club)
    db.refresh(req)

    # After the commit, and never allowed to fail it: the club exists whether or
    # not we manage to say so. Returned to the caller so the platform admin can
    # see "we couldn't email them" and follow up by hand, rather than assuming.
    email_outcome = club_emails.send_club_live(
        db,
        to=req.requester_email,
        requester_name=req.requester_name,
        club_name=club.name,
        club_url=club_app_url(club),
        is_admin=appointed is not None,
        systems=enabled_systems,
        club_night=req.club_night_day,
    )
    if email_outcome != "sent":
        print(f"[club-request {req.id}] welcome email: {email_outcome}")

    return {
        "club": club,
        "request": _club_request_dict(req),
        "appointed_super_admin": appointed,
        "enabled_systems": enabled_systems,
        "skipped_systems": skipped_systems,
        "skipped_systems_reason": skipped_systems_reason,
        "email": email_outcome,
    }


@router.get("/platform/clubs-health")
def platform_clubs_health(
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Per-club health signals for the platform dashboard, so a stuck or dormant
    club is visible at a glance. All aggregates are grouped queries (a fixed
    handful regardless of how many clubs there are), not per-club N+1."""
    clubs = db.exec(select(Club).order_by(Club.name)).all()

    def _counts(rows) -> dict[int, int]:
        return {r[0]: r[1] for r in rows}

    def _ids(rows) -> set[int]:
        # session.exec() on a single-column select yields scalars, not rows.
        return set(rows)

    systems = _counts(db.exec(
        select(ClubSystem.club_id, func.count()).where(ClubSystem.enabled == True).group_by(ClubSystem.club_id)
    ).all())
    super_admins = _counts(db.exec(
        select(User.club_id, func.count()).where(User.is_super_admin == True).group_by(User.club_id)
    ).all())
    scope_admins = _ids(db.exec(select(AdminRole.club_id).distinct()).all())
    webhooks = _ids(db.exec(select(ClubWebhook.club_id).distinct()).all())
    players = _counts(db.exec(
        select(Player.club_id, func.count()).where(Player.active == True).group_by(Player.club_id)
    ).all())
    last_signup = {r[0]: r[1] for r in db.exec(
        select(Signup.club_id, func.max(Signup.created_at)).group_by(Signup.club_id)
    ).all()}
    published = _ids(db.exec(
        select(PublishState.club_id).where(PublishState.published == True).distinct()
    ).all())

    return [
        {
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "active": c.active,
            "region": c.region,
            "systems_enabled": systems.get(c.id, 0),
            "super_admins": super_admins.get(c.id, 0),
            "has_scope_admin": c.id in scope_admins,
            "webhook": c.id in webhooks,
            "players": players.get(c.id, 0),
            "last_signup_at": last_signup.get(c.id),
            "pairings_published": c.id in published,
        }
        for c in clubs
    ]
