"""Database engine + table-level write guard.

We point at the existing Supabase Postgres via the transaction pooler. To stop
the new app accidentally corrupting production data while we're still building,
a `before_flush` listener raises on any attempted write to tables we haven't
explicitly opted in.

WRITE_ALLOWED_TABLES is the explicit allow-list. As we build out write features
table-by-table, we add the table name here.
"""
import os
from dotenv import load_dotenv
from sqlalchemy import event
from sqlmodel import Session, create_engine
from sqlalchemy.pool import NullPool

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# Tables the app is allowed to write to. Anything not in this set raises on flush.
WRITE_ALLOWED_TABLES: set[str] = {
    "users",          # auth: created on login, updated on claim-profile
    "signups",        # Call to Arms form: insert/update/delete own signup
    "pairings",       # drop-out flow deletes prearranged pairings involving the dropper
    "players",        # only write is inserting new players via create-profile
    "league_results", # result submission + full ratings recalc
    "league_ratings", # result submission + full ratings recalc
    "admin_roles",    # admin appointment/removal
    "pairing_blocks", # admin block add/remove
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