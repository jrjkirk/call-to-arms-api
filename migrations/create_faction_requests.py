"""Faction requests (2026-10-21): the faction_requests table.

One new table, nothing else touched. RUN THIS BEFORE DEPLOYING the faction
requests code: the new code writes to it when an admin asks for a faction. Old
code never reads it, so running it early is harmless.

Self-contained SQL, not models.FactionRequest, because it runs on the Fly
machine before the deploy, where models.py doesn't have that class yet. Mirrors
the model column for column.

    PYTHONPATH=. python migrations/create_faction_requests.py                # dry run
    PYTHONPATH=. python migrations/create_faction_requests.py --apply
    PYTHONPATH=. python migrations/create_faction_requests.py --verify-only

One transaction; idempotent (IF NOT EXISTS throughout).
"""
import sys

from sqlalchemy import text

from database import engine

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS faction_requests (
        id                   SERIAL PRIMARY KEY,
        created_at           TIMESTAMP NOT NULL DEFAULT now(),
        batch_id             VARCHAR NOT NULL,
        club_id              BIGINT NOT NULL REFERENCES clubs (id),
        system               VARCHAR NOT NULL,
        name                 VARCHAR NOT NULL,
        suggested_group      VARCHAR,
        note                 VARCHAR,
        status               VARCHAR NOT NULL DEFAULT 'pending',
        requested_by_user_id BIGINT NOT NULL REFERENCES users (id),
        requested_by_name    VARCHAR NOT NULL,
        approved_name        VARCHAR,
        decline_reason       VARCHAR,
        reviewed_at          TIMESTAMP,
        reviewed_by_user_id  BIGINT REFERENCES users (id),
        reviewed_by_name     VARCHAR
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_faction_requests_created_at ON faction_requests (created_at)",
    "CREATE INDEX IF NOT EXISTS ix_faction_requests_batch_id ON faction_requests (batch_id)",
    "CREATE INDEX IF NOT EXISTS ix_faction_requests_club_id ON faction_requests (club_id)",
    "CREATE INDEX IF NOT EXISTS ix_faction_requests_system ON faction_requests (system)",
    "CREATE INDEX IF NOT EXISTS ix_faction_requests_status ON faction_requests (status)",
    "CREATE INDEX IF NOT EXISTS ix_faction_requests_requested_by_user_id ON faction_requests (requested_by_user_id)",
]

EXPECTED_COLUMNS = {"id", "created_at", "batch_id", "club_id", "system", "name",
                    "suggested_group", "note", "status", "requested_by_user_id",
                    "requested_by_name", "approved_name", "decline_reason",
                    "reviewed_at", "reviewed_by_user_id", "reviewed_by_name"}


def verify(conn) -> list[str]:
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'faction_requests'"
    )).all()}
    if not cols:
        return ["faction_requests table does not exist"]
    problems = [f"missing column {c}" for c in sorted(EXPECTED_COLUMNS - cols)]
    indexes = [r[0] for r in conn.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'faction_requests'"
    )).all()]
    for col in ("club_id", "status", "system"):
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
            missing = verify(conn) == ["faction_requests table does not exist"]
        if missing:
            print("DRY RUN, nothing written. --apply would run:")
            for sql in STATEMENTS:
                print("  " + " ".join(sql.split()))
        else:
            print("DRY RUN, nothing written. faction_requests already exists; --apply would only re-check it.")
        return

    if problems:
        print(f"VERIFICATION FAILED: {problems}")
        sys.exit(1)
    print("Verification passed.")


if __name__ == "__main__":
    main()
