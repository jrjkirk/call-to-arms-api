"""Create scheduled_job_claims — the scheduler's mutual-exclusion table.

One row per (job_name, period_key) actually worked. The UNIQUE constraint is
the lock: whichever runner's INSERT lands first does the work, everyone else
takes an IntegrityError and does nothing. See database.claim_job_period() and
models.ScheduledJobClaim.

New table, so create_all(checkfirst=True) would in fact create it — but this
runs explicitly and idempotently, in the same style as every other venue/table
migration here, so production schema changes stay a deliberate step rather
than a side effect of a deploy.

    PYTHONPATH=. python migrations/create_scheduled_job_claims.py
    PYTHONPATH=. python migrations/create_scheduled_job_claims.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'scheduled_job_claims'"
        )).first()
        print(f"scheduled_job_claims: {'present' if exists else 'MISSING'}")

        if not exists and not verify_only:
            conn.execute(text("""
                CREATE TABLE scheduled_job_claims (
                    id          SERIAL PRIMARY KEY,
                    job_name    VARCHAR NOT NULL,
                    period_key  VARCHAR NOT NULL,
                    claimed_at  TIMESTAMP NOT NULL DEFAULT now(),
                    CONSTRAINT uq_job_claim_period UNIQUE (job_name, period_key)
                )
            """))
            print("  created scheduled_job_claims")

        if not verify_only:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scheduled_job_claims_job_name "
                "ON scheduled_job_claims (job_name)"))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_scheduled_job_claims_claimed_at "
                "ON scheduled_job_claims (claimed_at)"))

        # The constraint is the whole point of the table, so verify it by name
        # rather than trusting the table's existence.
        uq = conn.execute(text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE table_name = 'scheduled_job_claims' "
            "AND constraint_name = 'uq_job_claim_period'"
        )).first()
        print(f"uq_job_claim_period:  {'present' if uq else 'MISSING'}")

    if verify_only:
        sys.exit(0 if (exists and uq) else 1)
    print("\nDone.")


if __name__ == "__main__":
    main()
