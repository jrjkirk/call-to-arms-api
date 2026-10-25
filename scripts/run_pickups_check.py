"""Pick-up games check: close proposals nobody answered in time and expire
requests whose window has passed (pickups.sweep), across every club.

Runs from scheduler.py on its tick. Every read and answer in pickups.py sweeps
its own club first too, so this is what keeps a quiet club moving: the player
left waiting on someone who never answered goes back in the pool and can be
matched again without anyone having to open the app.

By hand: PYTHONPATH=. python scripts/run_pickups_check.py
"""
from sqlmodel import Session

from database import engine, record_job_run

JOB_NAME = "pickups_check"


def main() -> None:
    from pickups import sweep

    with Session(engine) as db:
        try:
            changed = sweep(db)
            record_job_run(db, JOB_NAME, status="ok", detail=f"{changed} change(s)")
        except Exception as exc:
            db.rollback()
            record_job_run(db, JOB_NAME, status="error", detail=str(exc)[:300])
            changed = 0
        db.commit()
    print(f"Pick-ups check: {changed} change(s)")


if __name__ == "__main__":
    main()
