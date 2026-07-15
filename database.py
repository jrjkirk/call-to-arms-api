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

from models import ClubWebhook

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


def scoped(model: Type[T], club_id: int) -> Select:
    """The only sanctioned way to query a club-owned table once the
    caller's club_id is known. Returns a SELECT pre-filtered to one club;
    chain further .where()/.order_by()/etc. onto it exactly as you would
    a plain select(Model). club_id must come from the authenticated
    caller's context (user.club_id) — never accept it from a request
    body."""
    return select(model).where(model.club_id == club_id)