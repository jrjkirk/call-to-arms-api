"""Account overhaul Slab 5 (2026-09-15): the login_tokens table for email links.

One new table, nothing else touched. RUN THIS BEFORE DEPLOYING SLAB 5: the new
code writes to it. Old code never reads it, so running it early is harmless.

Self-contained SQL, not models.LoginToken, because it runs on the Fly machine
before the deploy, where models.py doesn't have that class yet. Mirrors the
model column for column.

    PYTHONPATH=. python migrations/create_login_tokens.py                # dry run
    PYTHONPATH=. python migrations/create_login_tokens.py --apply
    PYTHONPATH=. python migrations/create_login_tokens.py --verify-only

One transaction; idempotent (IF NOT EXISTS throughout).
"""
import sys

from sqlalchemy import text

from database import engine

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS login_tokens (
        id          SERIAL PRIMARY KEY,
        token_hash  VARCHAR NOT NULL,
        email       VARCHAR NOT NULL,
        purpose     VARCHAR NOT NULL,
        user_id     BIGINT REFERENCES users (id),
        origin      VARCHAR NOT NULL,
        next_path   VARCHAR,
        ip_hash     VARCHAR,
        created_at  TIMESTAMP NOT NULL DEFAULT now(),
        expires_at  TIMESTAMP NOT NULL,
        used_at     TIMESTAMP
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_login_tokens_token_hash ON login_tokens (token_hash)",
    "CREATE INDEX IF NOT EXISTS ix_login_tokens_email ON login_tokens (email)",
    "CREATE INDEX IF NOT EXISTS ix_login_tokens_user_id ON login_tokens (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_login_tokens_ip_hash ON login_tokens (ip_hash)",
    "CREATE INDEX IF NOT EXISTS ix_login_tokens_created_at ON login_tokens (created_at)",
]

EXPECTED_COLUMNS = {"id", "token_hash", "email", "purpose", "user_id", "origin", "next_path",
                    "ip_hash", "created_at", "expires_at", "used_at"}


def verify(conn) -> list[str]:
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'login_tokens'"
    )).all()}
    if not cols:
        return ["login_tokens table does not exist"]
    problems = [f"missing column {c}" for c in sorted(EXPECTED_COLUMNS - cols)]
    indexes = [r[0] for r in conn.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'login_tokens'"
    )).all()]
    if not any("UNIQUE" in d and "(token_hash)" in d for d in indexes):
        problems.append("no UNIQUE index on token_hash")
    return problems


def main() -> None:
    if "--verify-only" in sys.argv:
        with engine.connect() as conn:
            problems = verify(conn)
    elif "--apply" in sys.argv:
        with engine.begin() as conn:
            for sql in STATEMENTS:
                conn.execute(text(sql))
            problems = verify(conn)
            if problems:
                raise RuntimeError(f"verification failed, rolling back: {problems}")
        print("APPLIED.")
    else:
        with engine.connect() as conn:
            missing = verify(conn) == ["login_tokens table does not exist"]
        if missing:
            print("DRY RUN, nothing written. --apply would run:")
            for sql in STATEMENTS:
                print("  " + " ".join(sql.split()))
        else:
            print("DRY RUN, nothing written. login_tokens already exists; --apply would only re-check it.")
        return

    if problems:
        print(f"VERIFICATION FAILED: {problems}")
        sys.exit(1)
    print("Verification passed.")


if __name__ == "__main__":
    main()
