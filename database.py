"""Database engine + table-level write guard.

We point at the existing Supabase Postgres via the transaction pooler. To stop
the new app accidentally corrupting production data while we're still building,
a `before_flush` listener raises on any attempted write to tables we haven't
explicitly opted in.

WRITE_ALLOWED_TABLES is the explicit allow-list. As we build out write features
table-by-table, we add the table name here.
"""
import os
from typing import Type, TypeVar

from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.sql import Select
from sqlmodel import Session, create_engine, select
from sqlalchemy.pool import NullPool

from models import Club, ClubWebhook, User

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
    "clubs",          # Phase 1 step 1: seeded once by seed_clubs.py, then
                       # read-only until a later Phase 1 step starts
                       # scoping queries by club
    "club_systems",   # Phase 1 step 1: seeded once by seed_clubs.py, then
                       # read-only until a later Phase 1 step starts
                       # scoping queries by club
    "club_settings",  # auto-pairings scheduler settings, now per-club
                       # (split out of app_settings) — admin.py's
                       # auto-pairings-settings endpoints + the scheduler
    "club_webhooks",  # Phase 3 step 1: seeded once by
                       # seed_club_webhooks.py, then read-only until a
                       # later Phase 3 step switches the six webhook call
                       # sites over to reading from here
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


def resolve_public_club_id(db: Session, club_slug: str | None) -> int:
    """The sanctioned way for the three genuinely public, unauthenticated
    endpoints (GET /pairings, GET /league/factions, GET /week-id) to
    resolve a club_id. Explicit slug takes precedence; omitted slug falls
    back to resolve_single_active_club_id unchanged, so every existing
    caller (none of which pass a slug yet) keeps today's behavior exactly.
    Raises ValueError for an unknown or inactive slug — deliberately the
    same message for both, so a 404 built from it never leaks which case
    applied (same obfuscation convention as admin.py's "not found or
    inactive" checks)."""
    if club_slug is None:
        return resolve_single_active_club_id(db)

    club = db.exec(select(Club).where(Club.slug == club_slug)).first()
    if club is None or not club.active:
        raise ValueError("Club not found.")
    return club.id


def resolve_request_club_id(db: Session, user: User | None, club_slug: str | None) -> int:
    """Resolve which club a request to the otherwise-public pairings pages
    (GET /pairings, GET /week-id, GET /league/factions) should be scoped to.

    If the request carries a valid authenticated session, that user's own
    club (user.club_id) is authoritative and the slug/hostname `club` param
    is ignored entirely. This closes the cross-club leak where a logged-in
    user browsing via the bare/default hostname (which the frontend maps to
    a hardcoded "manchester" slug) would otherwise be served another club's
    published data — e.g. a Yorkshire super admin seeing Manchester's Old
    World pairings, a system Yorkshire doesn't even run.

    Only genuinely anonymous requests (no session) fall back to slug-based
    resolve_public_club_id, preserving the anonymous shared-link behavior
    those public pages were deliberately built to support (an unauthenticated
    visitor following a link to a specific club's still-published pairings).
    Raises the same ValueError/RuntimeError as resolve_public_club_id in the
    anonymous path, so existing 404/500 handling at the call sites is
    unchanged."""
    if user is not None:
        return user.club_id
    return resolve_public_club_id(db, club_slug)


def scoped(model: Type[T], club_id: int) -> Select:
    """The only sanctioned way to query a club-owned table once the
    caller's club_id is known. Returns a SELECT pre-filtered to one club;
    chain further .where()/.order_by()/etc. onto it exactly as you would
    a plain select(Model). club_id must come from the authenticated
    caller's context (user.club_id) — never accept it from a request
    body."""
    return select(model).where(model.club_id == club_id)