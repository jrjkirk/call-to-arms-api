"""Account overhaul Slab 0 (2026-09-15): identities separate from accounts.

Expand only. Nothing is dropped or renamed, so the code running before the
deploy keeps working against the result. RUN THIS BEFORE DEPLOYING SLAB 0: the
new code reads users.display_name and users.session_version.

  1. CREATE TABLE user_identities (FK to users, UNIQUE (provider,
     provider_user_id), UNIQUE (user_id, provider) WHERE is_primary,
     index user_id)
  2. users: DROP NOT NULL on discord_id and discord_name
            ADD display_name TEXT
            ADD session_version INTEGER NOT NULL DEFAULT 0
  3. club_requests: ADD identity_provider TEXT, identity_subject TEXT (+ index)
  4. Backfill one primary 'discord' identity per user with a discord_id
  5. Backfill club_requests.identity_* from discord_id

All of it in one transaction: Postgres DDL is transactional, so a failure
anywhere leaves the database exactly as it was. Idempotent: every step checks
before it acts, so a re-run changes nothing.

session_version defaults to 0, which is the existing cookie format, so no one
is signed out by any of this.

    PYTHONPATH=. python migrations/create_user_identities.py                # dry run
    PYTHONPATH=. python migrations/create_user_identities.py --apply
    PYTHONPATH=. python migrations/create_user_identities.py --verify-only

Self-contained SQL, deliberately NOT importing models.UserIdentity: this runs
on the Fly machine BEFORE the deploy, where models.py doesn't have that class
yet. The DDL mirrors the model (models.UserIdentity) column for column.

Note: this Supabase project also has an (unused) auth.users table, so every
information_schema lookup below is pinned to table_schema = 'public'.
"""
import sys

from sqlalchemy import text

from database import engine

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_identities (
    id               SERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES users (id),
    provider         VARCHAR NOT NULL,
    provider_user_id VARCHAR NOT NULL,
    is_primary       BOOLEAN NOT NULL DEFAULT FALSE,
    email            VARCHAR,
    email_verified   BOOLEAN NOT NULL DEFAULT FALSE,
    name             VARCHAR,
    avatar_url       VARCHAR,
    created_at       TIMESTAMP NOT NULL DEFAULT now(),
    last_used_at     TIMESTAMP,
    CONSTRAINT uq_identity_provider_subject UNIQUE (provider, provider_user_id)
)
"""
CREATE_TABLE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_user_identities_user_id ON user_identities (user_id)",
    # At most one primary identity per provider per account.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_primary "
    "ON user_identities (user_id, provider) WHERE is_primary",
]

COLUMN_SQL = [
    ("users", "discord_id", "ALTER TABLE users ALTER COLUMN discord_id DROP NOT NULL", "nullable"),
    ("users", "discord_name", "ALTER TABLE users ALTER COLUMN discord_name DROP NOT NULL", "nullable"),
    ("users", "display_name", "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT", "exists"),
    ("users", "session_version",
     "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0", "exists"),
    ("club_requests", "identity_provider",
     "ALTER TABLE club_requests ADD COLUMN IF NOT EXISTS identity_provider TEXT", "exists"),
    ("club_requests", "identity_subject",
     "ALTER TABLE club_requests ADD COLUMN IF NOT EXISTS identity_subject TEXT", "exists"),
]

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_club_requests_identity_subject "
    "ON club_requests (identity_subject)"
)

BACKFILL_IDENTITIES_SQL = """
INSERT INTO user_identities
    (user_id, provider, provider_user_id, is_primary, email, email_verified, name, avatar_url, created_at, last_used_at)
SELECT u.id, 'discord', u.discord_id, TRUE, NULL, FALSE, u.discord_name, u.avatar_url,
       COALESCE(u.created_at, now()), u.last_login_at
FROM users u
WHERE u.discord_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM user_identities i
                  WHERE i.provider = 'discord' AND i.provider_user_id = u.discord_id)
  AND NOT EXISTS (SELECT 1 FROM user_identities i
                  WHERE i.user_id = u.id AND i.provider = 'discord')
"""

BACKFILL_REQUESTS_SQL = """
UPDATE club_requests
SET identity_provider = 'discord', identity_subject = discord_id
WHERE discord_id IS NOT NULL AND identity_subject IS NULL
"""


def _column(conn, table, column):
    return conn.execute(text(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).first()


def _table_exists(conn, table):
    return conn.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).first() is not None


def plan(conn) -> list[str]:
    """What --apply would do, from the database as it is now."""
    steps = []
    has_identities = _table_exists(conn, "user_identities")
    if not has_identities:
        steps.append("CREATE TABLE user_identities")
    for table, column, sql, done_when in COLUMN_SQL:
        col = _column(conn, table, column)
        done = col is not None if done_when == "exists" else (col is not None and col[0] == "YES")
        if not done:
            steps.append(sql)
    steps.append(INDEX_SQL + "  (IF NOT EXISTS)")

    users_with_discord = conn.execute(text(
        "SELECT count(*) FROM users WHERE discord_id IS NOT NULL")).scalar()
    already = conn.execute(text(
        "SELECT count(*) FROM user_identities WHERE provider = 'discord'")).scalar() if has_identities else 0
    steps.append(f"backfill discord identities: {users_with_discord} users with a discord_id, "
                 f"{already} identity rows already present")

    has_subject = _column(conn, "club_requests", "identity_subject") is not None
    pending = conn.execute(text(
        "SELECT count(*) FROM club_requests WHERE discord_id IS NOT NULL"
        + (" AND identity_subject IS NULL" if has_subject else "")
    )).scalar()
    steps.append(f"backfill club_requests identity: {pending} rows")
    return steps


def apply(conn) -> None:
    conn.execute(text(CREATE_TABLE_SQL))
    for sql in CREATE_TABLE_INDEX_SQL:
        conn.execute(text(sql))
    for table, column, sql, done_when in COLUMN_SQL:
        col = _column(conn, table, column)
        if done_when == "nullable" and col is not None and col[0] == "YES":
            continue
        conn.execute(text(sql))
    conn.execute(text(INDEX_SQL))
    n = conn.execute(text(BACKFILL_IDENTITIES_SQL)).rowcount
    print(f"  inserted {n} discord identities")
    n = conn.execute(text(BACKFILL_REQUESTS_SQL)).rowcount
    print(f"  backfilled {n} club requests")


def verify(conn) -> list[str]:
    problems = []
    if not _table_exists(conn, "user_identities"):
        return ["user_identities table does not exist"]
    for table, column, _, done_when in COLUMN_SQL:
        col = _column(conn, table, column)
        if col is None:
            problems.append(f"{table}.{column} missing")
        elif done_when == "nullable" and col[0] != "YES":
            problems.append(f"{table}.{column} is still NOT NULL")
    uniques = {r[0] for r in conn.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'user_identities'"
    )).all()}
    if not any("UNIQUE" in d and "(provider, provider_user_id)" in d for d in uniques):
        problems.append("user_identities has no UNIQUE (provider, provider_user_id)")
    if not any("UNIQUE" in d and "(user_id, provider)" in d and "is_primary" in d for d in uniques):
        problems.append("user_identities has no partial UNIQUE (user_id, provider) WHERE is_primary")
    missing = conn.execute(text(
        "SELECT count(*) FROM users u WHERE u.discord_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM user_identities i WHERE i.user_id = u.id AND i.provider = 'discord' "
        " AND i.provider_user_id = u.discord_id)"
    )).scalar()
    if missing:
        problems.append(f"{missing} users with a discord_id have no matching identity")
    stale = conn.execute(text(
        "SELECT count(*) FROM club_requests WHERE discord_id IS NOT NULL AND identity_subject IS NULL"
    )).scalar()
    if stale:
        problems.append(f"{stale} club requests not backfilled")
    return problems


def main() -> None:
    if "--verify-only" in sys.argv:
        with engine.connect() as conn:
            problems = verify(conn)
    elif "--apply" in sys.argv:
        with engine.begin() as conn:
            print("Applying:")
            apply(conn)
            problems = verify(conn)
            if problems:
                raise RuntimeError(f"verification failed, rolling back: {problems}")
        print("APPLIED.")
    else:
        with engine.connect() as conn:
            print("DRY RUN, nothing written. --apply would:")
            for step in plan(conn):
                print(f"  - {step}")
        return

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nVerification passed.")


if __name__ == "__main__":
    main()
