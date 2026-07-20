"""Database engine + table-level write guard.

We point at the existing Supabase Postgres via the transaction pooler. To stop
the new app accidentally corrupting production data while we're still building,
a `before_flush` listener raises on any attempted write to tables we haven't
explicitly opted in.

WRITE_ALLOWED_TABLES is the explicit allow-list. As we build out write features
table-by-table, we add the table name here.
"""
import os
from typing import Optional, Type, TypeVar
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.sql import Select
from sqlmodel import Session, create_engine, select
from sqlalchemy.pool import NullPool

from models import AuditLogEntry, Club, ClubSetting, ClubWebhook, PlatformBanner, ScheduledJobRun, User

T = TypeVar("T")

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# Tables the app is allowed to write to. Anything not in this set raises on flush.
WRITE_ALLOWED_TABLES: set[str] = {
    "users",          # auth: created on login, updated on claim-profile
    "signups",        # Call to Arms form: insert/update/delete own signup; also pairing grid save-back
    "pairings",       # drop-out flow + admin pairing generation/editing/deletion
    "publish_state",  # admin publish/unpublish pairings
    "players",        # only write is inserting new players via create-profile
    "league_results", # result submission + full ratings recalc
    "league_ratings", # result submission + full ratings recalc
    "admin_roles",    # admin appointment/removal
    "pairing_blocks", # admin block add/remove
    "app_settings",   # auto-pairings scheduler updates last_week after each run
    "systems",        # Phase 0 systems-as-data catalogue: seeded once by
                       # seed_systems_config.py, then read-only until the
                       # systems_from_catalogue flag flips app code onto it
    "clubs",          # Phase 1 step 1: seeded once by seed_clubs.py; now also
                       # written by the club super-admin editing the Club
                       # landing page profile (blurb/logo/links/hours)
    "club_systems",   # Phase 1 step 1: seeded once by seed_clubs.py; now also
                       # written by each system's own admin editing that
                       # system's Club-page carousel card
    "club_events",    # Club landing page calendar: one-off/override events,
                       # CRUD by club super-admin (system_id=None) or that
                       # system's own admin (system_id set)
    "club_settings",  # auto-pairings scheduler settings, now per-club
                       # (split out of app_settings) — admin.py's
                       # auto-pairings-settings endpoints + the scheduler
    "club_webhooks",  # Phase 3 step 1: seeded once by
                       # seed_club_webhooks.py, then read-only until a
                       # later Phase 3 step switches the six webhook call
                       # sites over to reading from here
    "missions",       # per-club-system random mission pool: admin CRUD in
                       # admin.py (image uploaded to Supabase Storage), read
                       # by the Call-to-Arms post to pick a random mission
    "league_seasons", # per-(club,system) league seasons (admin-set date
                       # ranges); ratings reset each season
    "league_configs", # per-(club,system) league scoring config (elo/winloss
                       # params); one row per system-league
    "platform_banner",    # site-wide announcement banner, platform-admin only
    "scheduled_job_runs", # cron heartbeat, written by the two scheduler
                           # scripts on every invocation
    "audit_log_entries",  # platform-wide "who changed X" log, appended by
                           # admin.py's mutation endpoints
    "club_requests",      # "please add my club" submissions from the
                           # logged-out hero page; reviewed (approve/deny)
                           # by a platform admin
}

engine = create_engine(DATABASE_URL, poolclass=NullPool, echo=False)


@event.listens_for(Session, "before_flush")
def _block_unallowed_writes(session, flush_context, instances):
    """Raise if any pending change touches a table not in WRITE_ALLOWED_TABLES."""
    pending = list(session.new) + list(session.dirty) + list(session.deleted)
    for obj in pending:
        table_name = getattr(obj.__class__, "__tablename__", None)
        if table_name and table_name not in WRITE_ALLOWED_TABLES:
            raise RuntimeError(
                f"Write to '{table_name}' is not currently permitted. "
                f"Allowed tables: {sorted(WRITE_ALLOWED_TABLES)}"
            )


def get_session():
    """FastAPI dependency: yields a database session that closes itself."""
    with Session(engine) as session:
        yield session


def resolve_webhook_url(
    db: Session, club_id: int, webhook_type: str, system_id: int | None = None
) -> str | None:
    """The sanctioned way to look up a club's configured Discord webhook URL.
    Returns the matching ClubWebhook.url, or None if no row exists — callers
    decide what fallback (if any) applies when this returns None."""
    row = db.exec(
        select(ClubWebhook).where(
            ClubWebhook.club_id == club_id,
            ClubWebhook.webhook_type == webhook_type,
            ClubWebhook.system_id == system_id,
        )
    ).first()
    return row.url if row else None


def resolve_single_active_club_id(db: Session) -> int:
    """Resolve the one active club, for callers with no other way to know
    which club they're serving (no authenticated user, no subdomain
    routing yet — see multitenancy-plan-v2.md's Phase 3/4). Raises rather
    than guessing if that's ever not true, so a second active club fails
    loudly instead of silently mixing clubs' data. Shared by
    post_league_rankings_image.py and the two unscoped public endpoints
    (GET /pairings, GET /league/factions); post_pairings_image.py's
    _resolve_single_club_id is intentionally not unified with this one —
    it also needs a specific system, not just "any active club"."""
    clubs = db.exec(select(Club).where(Club.active == True)).all()
    if len(clubs) != 1:
        raise RuntimeError(
            f"Cannot resolve a single active club — found {len(clubs)}, expected exactly 1. "
            f"No club selector exists yet for this caller; needs a real design decision "
            f"(e.g. subdomain-based resolution) once a second active club exists."
        )
    return clubs[0].id


# Mirrors call-to-arms-web's src/lib/clubSlug.ts exactly — the bare/www
# domain (and anything else this can't parse a subdomain from) has always
# meant Manchester, preserving every existing bookmark/QR-code link.
_PRIMARY_DOMAIN = "calltoarms.app"
_DEFAULT_CLUB_SLUG = "manchester"


def resolve_club_slug_from_origin(origin_header: str | None) -> str | None:
    """Derive a club slug from a browser request's Origin header
    (e.g. "https://yorkshire.calltoarms.app" -> "yorkshire"). This is what
    makes subdomain-based resolution real rather than the frontend having
    to remember to attach a `club` query param on every fetch: a browser
    sets Origin itself on every cross-origin request, so any current or
    future client-side call to a public endpoint resolves correctly with
    zero frontend code needed, not just the call sites someone remembered
    to update.

    Returns None (not a raise) when nothing usable is present — a missing
    Origin (server-to-server calls, curl, SSR loaders — see
    resolve_public_club_id) or an unparseable one — so callers can fall
    further back rather than treating this as an error."""
    if not origin_header:
        return None
    try:
        host = (urlparse(origin_header).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host == _PRIMARY_DOMAIN or host == f"www.{_PRIMARY_DOMAIN}":
        return _DEFAULT_CLUB_SLUG
    suffix = f".{_PRIMARY_DOMAIN}"
    if host.endswith(suffix):
        return host[: -len(suffix)] or _DEFAULT_CLUB_SLUG
    return None


def resolve_public_club_id(db: Session, club_slug: str | None, origin_header: str | None = None) -> int:
    """The sanctioned way for the three genuinely public, unauthenticated
    endpoints (GET /pairings, GET /league/factions, GET /week-id) to
    resolve a club_id, in order of precedence:
    1. An explicit `club` query param, if given (SSR loaders that can't
       carry a real browser Origin, manual testing/tooling).
    2. The request's Origin header, subdomain-parsed (see
       resolve_club_slug_from_origin) — the real fix: any genuine
       browser call resolves correctly without the frontend needing to
       compute/attach anything.
    3. resolve_single_active_club_id — the original stopgap, now only a
       last resort for a caller with neither (e.g. bare curl with no
       Origin and no club param).
    Raises ValueError for an unknown or inactive slug — deliberately the
    same message for both, so a 404 built from it never leaks which case
    applied (same obfuscation convention as admin.py's "not found or
    inactive" checks)."""
    if club_slug is None:
        club_slug = resolve_club_slug_from_origin(origin_header)
    if club_slug is None:
        return resolve_single_active_club_id(db)

    club = db.exec(select(Club).where(Club.slug == club_slug)).first()
    if club is None or not club.active:
        raise ValueError("Club not found.")
    return club.id


def resolve_request_club_id(
    db: Session, user: User | None, club_slug: str | None, origin_header: str | None = None
) -> int:
    """Resolve which club a request to the otherwise-public pairings pages
    (GET /pairings, GET /week-id, GET /league/factions) should be scoped to.

    If the request carries a valid authenticated session, that user's own
    club (user.club_id) is authoritative and the slug/Origin are ignored
    entirely. This closes the cross-club leak where a logged-in user
    browsing via the bare/default hostname (which resolves to "manchester")
    would otherwise be served another club's published data — e.g. a
    Yorkshire super admin seeing Manchester's Old World pairings, a system
    Yorkshire doesn't even run.

    Only genuinely anonymous requests (no session) fall back to
    resolve_public_club_id (explicit slug, then Origin, then the
    single-active-club stopgap), preserving the anonymous shared-link
    behavior those public pages were deliberately built to support (an
    unauthenticated visitor following a link to a specific club's still-
    published pairings). Raises the same ValueError/RuntimeError as
    resolve_public_club_id in the anonymous path, so existing 404/500
    handling at the call sites is unchanged."""
    if user is not None:
        return user.club_id
    return resolve_public_club_id(db, club_slug, origin_header)


def scoped(model: Type[T], club_id: int) -> Select:
    """The only sanctioned way to query a club-owned table once the
    caller's club_id is known. Returns a SELECT pre-filtered to one club;
    chain further .where()/.order_by()/etc. onto it exactly as you would
    a plain select(Model). club_id must come from the authenticated
    caller's context (user.club_id) — never accept it from a request
    body."""
    return select(model).where(model.club_id == club_id)

# ---------------------------------------------------------------------------
# Per-club-system settings helpers (ClubSetting)
#
# Shared by admin.py and the scheduler scripts (run_auto_pairings_check.py,
# run_call_to_arms_check.py), which previously each defined identical private
# copies. Defined once here (the shared-helper home, alongside scoped /
# _default_club_id); callers import them.
# ---------------------------------------------------------------------------

def system_setting_slug(system: str) -> str:
    """Settings-key-safe slug for a system's legacy name (spaces/apostrophes
    stripped) — used to build per-club-system ClubSetting keys like
    `call_to_arms_TheOldWorld_enabled`. Distinct from SystemConfig.slug
    (tow/hh/kt), which is a different, catalogue-facing identifier."""
    return system.replace(" ", "").replace("'", "")


def get_setting(db: Session, club_id: int, key: str, default: Optional[str] = None) -> Optional[str]:
    row = db.get(ClubSetting, (club_id, key))
    return row.value if row is not None else default


def upsert_setting(db: Session, club_id: int, key: str, value: str) -> None:
    row = db.get(ClubSetting, (club_id, key))
    if row is None:
        row = ClubSetting(club_id=club_id, key=key, value=value)
    else:
        row.value = value
    db.add(row)


# ---------------------------------------------------------------------------
# Platform admin tools: scheduled-job heartbeat + audit log helpers. Shared
# by admin.py (audit log) and the two scheduler scripts (job heartbeat),
# same "define once here" convention as the ClubSetting helpers above.
# ---------------------------------------------------------------------------

def record_job_run(db: Session, job_name: str, status: str, detail: Optional[str] = None) -> None:
    """Append a heartbeat row for one invocation of a scheduled job. Callers
    commit their own session — this only adds, matching upsert_setting's
    convention of leaving the commit to the caller (both scheduler scripts
    already commit once per club/system inside their loop)."""
    db.add(ScheduledJobRun(job_name=job_name, status=status, detail=detail))


def log_audit(
    db: Session, actor: User, action: str,
    target_type: Optional[str] = None, target_id: Optional[int] = None,
    detail: Optional[str] = None,
) -> None:
    """Append one audit-log row for a notable admin mutation. Caller commits
    (same convention as record_job_run/upsert_setting) — call this right
    before the endpoint's own db.commit() so the log entry lands in the
    same transaction as the change it's recording."""
    db.add(AuditLogEntry(
        actor_user_id=actor.id,
        actor_name=actor.discord_name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    ))
