"""In-app notices (2026-09-17): the player_notices table.

One new table, nothing else touched. RUN THIS BEFORE DEPLOYING the notices
code: the new code writes to it on publish and on a drop-out. Old code never
reads it, so running it early is harmless.

Self-contained SQL, not models.PlayerNotice, because it runs on the Fly machine
before the deploy, where models.py doesn't have that class yet. Mirrors the
model column for column.

    PYTHONPATH=. python migrations/create_player_notices.py                # dry run
    PYTHONPATH=. python migrations/create_player_notices.py --apply
    PYTHONPATH=. python migrations/create_player_notices.py --verify-only

One transaction; idempotent (IF NOT EXISTS throughout).
"""
import sys

from sqlalchemy import text

from database import engine

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS player_notices (
        id          SERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL REFERENCES users (id),
        club_id     BIGINT NOT NULL REFERENCES clubs (id),
        system      VARCHAR NOT NULL,
        week        VARCHAR NOT NULL,
        kind        VARCHAR NOT NULL,
        body        VARCHAR NOT NULL,
        created_at  TIMESTAMP NOT NULL DEFAULT now(),
        seen_at     TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_player_notices_user_id ON player_notices (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_player_notices_club_id ON player_notices (club_id)",
    "CREATE INDEX IF NOT EXISTS ix_player_notices_created_at ON player_notices (created_at)",
]

EXPECTED_COLUMNS = {"id", "user_id", "club_id", "system", "week", "kind", "body",
                    "created_at", "seen_at"}


def verify(conn) -> list[str]:
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'player_notices'"
    )).all()}
    if not cols:
        return ["player_notices table does not exist"]
    problems = [f"missing column {c}" for c in sorted(EXPECTED_COLUMNS - cols)]
    indexes = [r[0] for r in conn.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'player_notices'"
    )).all()]
    for col in ("user_id", "club_id"):
        if not any(f"({col})" in d for d in indexes):
            problems.append(f"no index on {col}")
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
            missing = verify(conn) == ["player_notices table does not exist"]
        if missing:
            print("DRY RUN, nothing written. --apply would run:")
            for sql in STATEMENTS:
                print("  " + " ".join(sql.split()))
        else:
            print("DRY RUN, nothing written. player_notices already exists; --apply would only re-check it.")
        return

    if problems:
        print(f"VERIFICATION FAILED: {problems}")
        sys.exit(1)
    print("Verification passed.")


if __name__ == "__main__":
    main()
