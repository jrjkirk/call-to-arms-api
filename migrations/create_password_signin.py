"""Account overhaul Slab 8 (2026-09-16): tables for password sign-in.

Two new tables and one new column, nothing else touched. RUN THIS BEFORE
DEPLOYING SLAB 8: the new code reads and writes all three. Old code never
looks at them, so running it early is harmless.

  1. password_credentials  one Argon2id hash per account (passwords.py)
  2. login_attempts        failed password attempts, for the guessing limits
  3. login_tokens.secret   holds the Argon2 hash of a password between signing
                           up and clicking the confirmation email, so an
                           account only exists once its address is confirmed

Self-contained SQL, not the models, because it runs on the Fly machine before
the deploy, where models.py doesn't have these yet. Mirrors them column for
column. One transaction; idempotent.

    PYTHONPATH=. python migrations/create_password_signin.py                # dry run
    PYTHONPATH=. python migrations/create_password_signin.py --apply
    PYTHONPATH=. python migrations/create_password_signin.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS password_credentials (
        id         SERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL REFERENCES users (id),
        hash       VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT now(),
        updated_at TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_password_credentials_user_id ON password_credentials (user_id)",
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id         SERIAL PRIMARY KEY,
        scope      VARCHAR NOT NULL,
        key_hash   VARCHAR NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_login_attempts_key_hash ON login_attempts (key_hash)",
    "CREATE INDEX IF NOT EXISTS ix_login_attempts_created_at ON login_attempts (created_at)",
    "ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS secret VARCHAR",
]

EXPECTED = {
    "password_credentials": {"id", "user_id", "hash", "created_at", "updated_at"},
    "login_attempts": {"id", "scope", "key_hash", "created_at"},
}


def _columns(conn, table):
    return {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()}


def verify(conn) -> list[str]:
    problems = []
    for table, cols in EXPECTED.items():
        have = _columns(conn, table)
        if not have:
            problems.append(f"{table} does not exist")
            continue
        problems += [f"{table} missing {c}" for c in sorted(cols - have)]
    if "secret" not in _columns(conn, "login_tokens"):
        problems.append("login_tokens.secret missing")
    indexes = [r[0] for r in conn.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'password_credentials'"
    )).all()]
    if not any("UNIQUE" in d and "(user_id)" in d for d in indexes):
        problems.append("password_credentials has no UNIQUE index on user_id")
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
            problems = verify(conn)
        print("DRY RUN, nothing written. --apply would run:" if problems
              else "DRY RUN, nothing written. Everything is already in place; --apply would only re-check it.")
        if problems:
            for sql in STATEMENTS:
                print("  " + " ".join(sql.split()))
            print("  (outstanding: " + "; ".join(problems) + ")")
        return

    if problems:
        print(f"VERIFICATION FAILED: {problems}")
        sys.exit(1)
    print("Verification passed.")


if __name__ == "__main__":
    main()
