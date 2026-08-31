"""Release unpaid ticket holds that have lapsed, and move the waitlist up.

Runs from the in-process scheduler (see scheduler.py). Cheap and idempotent:
it only touches entries whose hold has actually passed, so running it every
five minutes costs one indexed query when there is nothing to do.

    PYTHONPATH=. python scripts/run_ticket_holds_check.py
"""
from sqlmodel import Session

import tickets
from database import engine, record_job_run

JOB_NAME = "ticket_holds_check"


def main() -> None:
    with Session(engine) as db:
        try:
            result = tickets.expire_holds(db)
            if result["expired"] or result["promoted"]:
                print(f"Ticket holds — released {result['expired']}, "
                      f"promoted {result['promoted']} off waiting lists")
            record_job_run(db, JOB_NAME, "ok",
                           f"expired={result['expired']} promoted={result['promoted']}")
            db.commit()
        except Exception as exc:
            db.rollback()
            import traceback
            traceback.print_exc()
            record_job_run(db, JOB_NAME, "error", str(exc)[:500])
            db.commit()
            raise


if __name__ == "__main__":
    main()
