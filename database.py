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

from models import AuditLogEntry, Club, ClubSetting, ClubWebhook, PlatformBanner, Player, ScheduledJobRun, User

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
                       # system's Club-page carousel card and its per-system
                       # Discord server / gate mode
    "player_discord_verifications",  # per-(player, guild) Discord membership
                       # cache for the signup gate — inserted once per player
                       # per server by signups.require_discord_member
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
    "pairing_configs", # per-(club,system) pairing weighting config (admin-set
                        # sliders for mirror/rematch/vibe/experience/eta/
                        # scenario/points); read by pairings_engine.generate()
    "platform_banner",    # site-wide announcement banner, platform-admin only
    "scheduled_job_runs", # cron heartbeat, written by the two scheduler
                           # scripts on every invocation
    "audit_log_entries",  # platform-wide "who changed X" log, appended by
                           # admin.py's mutation endpoints
    "club_requests",      # "please add my club" submissions from the
                           # logged-out hero page; reviewed (approve/deny)
                           # by a platform admin
    "table_booking_configs",       # per-(club,system) venue table-booking
                                    # email config, admin CRUD in admin.py
    "table_booking_notifications", # audit trail + idempotency guard for
                                    # sent table-booking emails
    "call_outs",       # ad-hoc "call to arms": player-posted open game
                       # requests (create/take/cancel in call_outs.py, daily
                       # reminder + auto-expire in run_call_outs_check.py)
}

# Bounded client-side pool against Supabase's TRANSACTION pooler (port 6543).
#
# This was NullPool, which opened a brand new connection — TLS handshake and
# all — for every single request. The admin page fires ~40 requests in parallel
# on load, so it asked the pooler for ~40 simultaneous fresh connections; the
# pooler ran out and dropped them, which is the `SSL connection has been closed
# unexpectedly` wave that used to hit the admin page (was KNOWN_ISSUES #2).
#
# Client-side pooling is safe with the transaction pooler HERE specifically:
# psycopg2 doesn't use server-side prepared statements, which is the usual
# transaction-mode hazard (it's what bites asyncpg). Do not switch drivers
# without revisiting this.
#
# SIZING — read this before changing either number.
#
# A connection is acquired during DEPENDENCY resolution (require_user does
# `db.get(User, ...)`) and held until the request finishes and get_session's
# generator tears down. So the number of simultaneously checked-out connections
# tracks **in-flight requests**, not executing handlers.
#
# An earlier version of this comment claimed AnyIO's 40-thread limiter capped
# it, and sized the pool to 40 on that basis. That was wrong, and it 500'd in
# production with `QueuePool limit of size 10 overflow 30 reached`: a request
# holds its connection while it is merely QUEUED for a thread, so 60 concurrent
# requests want 60 connections no matter how few threads run at once.
#
# THE REAL CEILING IS SUPABASE'S, AND IT IS LOW. Measured against the prod
# pooler from this machine, holding N connections simultaneously for 2s:
#
#     12 -> 24/24 clean        25 -> 48/50
#     16 -> 32/32 clean        30 -> 56/60
#     20 -> 39/40              100 -> 89/100
#
# Past ~16 the pooler starts dropping them, and the failure it returns is
# exactly `SSL connection has been closed unexpectedly` — the error this whole
# saga started with. So a BIGGER pool was never going to help: NullPool failed
# by asking for ~40 at once, and a 40-capacity pool would fail the same way.
# Total capacity must stay under that ceiling, full stop.
#
# What keeps requests from queueing behind 14 connections is not pool size but
# not making 60 requests at once — the admin page now loads only the system
# being viewed (see ensureSystemScope in the web repo). pool_timeout is
# generous so a brief queue waits rather than 500s.
#
# One uvicorn process (no --workers), so this pool is the whole application's
# footprint. Scheduled scripts run as separate processes with their own pools,
# hence leaving a couple of connections of headroom under 16.
#
# pool_size is the warm baseline, deliberately most of the capacity: a reused
# connection can't be refused, so minimising new-connection churn minimises
# exposure to the drop.
#
# pool_pre_ping: pgbouncer and the network both drop idle connections, and a
# pooled connection that died while idle would otherwise surface as a random
# query error. The ping costs a round trip on checkout and buys immunity to
# stale-connection errors.
# pool_recycle: proactively retire connections after 5 minutes so they rarely
# reach the state pre_ping has to catch.
# pool_timeout: fail in 10s rather than hanging a request forever if all 15 are
# checked out.
#
# Applied only for PostgreSQL. SQLite uses SingletonThreadPool, which rejects
# max_overflow/pool_timeout outright — and `sqlite:///…` is now the local dev
# and test path (the staging Postgres project no longer exists), so passing
# these unconditionally breaks every local run.
# Measured safe ceiling on simultaneous connections to Supabase's pooler (see
# above). Total capacity stays under it, with headroom for cron processes.
SUPABASE_SAFE_CONNECTIONS = 16
_POOL_WARM = 10
_POOL_OVERFLOW = 4

_POOL_KWARGS = (
    {
        "pool_size": _POOL_WARM,
        "max_overflow": _POOL_OVERFLOW,
        "pool_pre_ping": True,
        "pool_recycle": 300,
        # Requests WILL briefly queue here under a burst — that's the design,
        # since capacity is capped by Supabase rather than by demand. Generous
        # so a queue waits instead of 500ing; a burst of 40 short requests
        # drains through 14 connections in well under a second.
        "pool_timeout": 30,
    }
    if DATABASE_URL.startswith(("postgresql", "postgres://"))
    else {}
)

engine = create_engine(DATABASE_URL, echo=False, **_POOL_KWARGS)


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


def discord_mentions_for_player_ids(
    db: Session, player_ids: list[int]
) -> dict[int, str]:
    """Map player_id -> a Discord mention string (`<@discord_id>`) for every
    player that is linked to a Discord account.

    Players with no linked account are simply absent from the result, so
    callers fall back to the plain name — that covers pre-seeded roster
    entries nobody has claimed and guest/+1 signups (which have no player
    row at all). Ownership is read from `Player.user_id`, the network-model
    link, not the legacy `User.player_id` (which only ever covered a user's
    home club).
    """
    ids = [pid for pid in player_ids if pid]
    if not ids:
        return {}
    players = db.exec(
        select(Player).where(Player.id.in_(ids)).where(Player.user_id.is_not(None))
    ).all()
    user_ids = {p.user_id for p in players}
    if not user_ids:
        return {}
    users = db.exec(select(User).where(User.id.in_(user_ids))).all()
    discord_by_user_id = {u.id: u.discord_id for u in users if u.discord_id}
    return {
        p.id: f"<@{discord_by_user_id[p.user_id]}>"
        for p in players
        if p.user_id in discord_by_user_id
    }


def name_with_mention(db: Session, player_name: str, player_id: Optional[int]) -> str:
    """`**Name** (<@discord_id>)` when the player is linked to a Discord
    account, plain `**Name**` otherwise (unclaimed roster entries, guests).

    The shared format for every Discord post that names a player — signups,
    drops, byes and call-outs — so a tag always reads the same way.
    """
    if player_id is None:
        return f"**{player_name}**"
    mention = discord_mentions_for_player_ids(db, [player_id]).get(player_id)
    return f"**{player_name}** ({mention})" if mention else f"**{player_name}**"


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
# The club the bare/www/default domain resolves to. Renamed manchester->egnwgc
# 2026-07-26 (club slugs rebranded to egnwgc / theoutpost); the DB slug must
# match this. Mirrors call-to-arms-web src/lib/clubSlug.ts DEFAULT_CLUB_SLUG.
_DEFAULT_CLUB_SLUG = "egnwgc"


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


def resolve_active_club_slug_from_origin(origin_header: str | None) -> str | None:
    """Like resolve_club_slug_from_origin, but for the AUTHENTICATED
    active-club path — the multi-club "network" model (2026-07-25) where a
    logged-in user can act at any club, chosen by which subdomain they're on.

    Crucial difference: the bare/www domain returns None here, NOT
    "manchester". For an authenticated request the bare domain means "no
    explicit club chosen" → the resolver falls back to the user's own soft
    home club. Only a genuine `X.calltoarms.app` subdomain counts as the user
    deliberately choosing to act at club X. (resolve_club_slug_from_origin
    keeps its manchester-default for the anonymous public pages, which have no
    user home club to fall back to — that behavior is unchanged.)"""
    if not origin_header:
        return None
    try:
        host = (urlparse(origin_header).hostname or "").lower()
    except ValueError:
        return None
    if not host or host == _PRIMARY_DOMAIN or host == f"www.{_PRIMARY_DOMAIN}":
        return None
    suffix = f".{_PRIMARY_DOMAIN}"
    if host.endswith(suffix):
        return host[: -len(suffix)] or None
    return None


def resolve_active_club_id(
    db: Session, user: User, origin_header: str | None = None, club_slug: str | None = None
) -> int:
    """The active club for an authenticated request in the multi-club network
    model: the club whose subdomain the user is browsing, else their soft home
    club. Playing is open to every club, so — unlike resolve_request_club_id,
    which predates this model and forces user.club_id — we deliberately honor
    the subdomain. Authorization is a separate concern: admin endpoints still
    gate on admin_roles for whatever club this resolves to, so honoring the
    subdomain grants a travelling player no admin rights at the host club.

    An explicit `club_slug` (rare — tooling/SSR) wins over the Origin. An
    unknown/inactive slug is ignored (falls back to home) rather than raising,
    since a stale subdomain shouldn't 500 a logged-in user's whole session."""
    slug = club_slug or resolve_active_club_slug_from_origin(origin_header)
    if slug is not None:
        club = db.exec(select(Club).where(Club.slug == slug, Club.active == True)).first()
        if club is not None:
            return club.id
    # Fall back to the soft home club, then the legacy registration club.
    return user.home_club_id or user.club_id


def active_player_id_for(db: Session, user: User, club_id: int) -> Optional[int]:
    """The id of the Player this user owns at a given club (their identity
    *there*), or None if they have no player at that club yet — in which case
    the frontend shows the claim/create-a-profile flow, exactly as it does
    today for a brand-new user. Multi-club network model: a user owns one
    Player per club (Player.user_id), so "my player" is now club-relative."""
    row = db.exec(
        select(Player).where(
            Player.user_id == user.id,
            Player.club_id == club_id,
            Player.active == True,
        )
    ).first()
    return row.id if row else None


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

    Multi-club network model (2026-07-25): for an authenticated session the
    active club is resolved from the subdomain the user is on (Origin), falling
    back to their soft home club — see resolve_active_club_id. This deliberately
    REVERSES the pre-network behavior (which forced user.club_id and ignored the
    Origin): playing is open at every club, so a logged-in user browsing
    yorkshire.calltoarms.app/pairings should see Yorkshire's pairings, not their
    home club's. The bare/default hostname still resolves to the user's home
    club (resolve_active_club_slug_from_origin returns None there), so every
    existing Manchester bookmark/QR link is unchanged.

    Only genuinely anonymous requests (no session) fall back to
    resolve_public_club_id (explicit slug, then Origin, then the
    single-active-club stopgap), preserving the anonymous shared-link
    behavior those public pages were deliberately built to support (an
    unauthenticated visitor following a link to a specific club's still-
    published pairings). Raises the same ValueError/RuntimeError as
    resolve_public_club_id in the anonymous path, so existing 404/500
    handling at the call sites is unchanged."""
    if user is not None:
        return resolve_active_club_id(db, user, origin_header, club_slug)
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
