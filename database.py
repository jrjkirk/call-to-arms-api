import os
from sqlmodel import create_engine, Session
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

load_dotenv()  # In local dev, reads .env file. In production (Fly), env vars come from secrets.

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add it to .env locally or set as a Fly secret in production.")

# NullPool: don't pool connections client-side. Supabase's transaction pooler handles pooling.
engine = create_engine(DATABASE_URL, echo=False, poolclass=NullPool)

# Read-only guard: refuse to commit anything until we deliberately enable writes later.
READ_ONLY = True

def get_session():
    """FastAPI dependency: opens a session per request, closes it after.
    
    If READ_ONLY is True, we hook into the session to block any flushes (i.e. writes)
    from reaching the database. This is a safety net while we're early in the migration —
    it makes it impossible to accidentally mutate production data.
    """
    with Session(engine) as session:
        if READ_ONLY:
            @event.listens_for(session, "before_flush")
            def _block_flush(session, flush_context, instances):
                raise RuntimeError("Database is in READ_ONLY mode. No writes permitted.")
        yield session

# Late import to keep the file readable
from sqlalchemy import event