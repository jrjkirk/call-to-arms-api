"""Call-outs check — invoked by its own GitHub Actions workflow (hourly, same
cadence as the other scheduler jobs).

For every OPEN call-out (see models.CallOut / call_outs.py):
  - if its game time has passed, mark it "expired" (silently — no chatter);
  - otherwise, if it hasn't been reminded about in the last 24h, re-post it to
    the club+system's Discord channel and stamp last_reminder_at.

Per-club/per-system isolation is inherent: each call-out carries its own
club_id + system, and the webhook is resolved from that (call_outs._post_call_out
→ resolve_webhook_url), so a club only ever hears about its own call-outs.

Safe to re-run hourly: expiry is idempotent (already-expired rows aren't
"open"), and the 24h last_reminder_at guard stops duplicate reminders.
"""
from datetime import timedelta

from sqlmodel import Session, select

from database import engine, name_with_mention, record_job_run
from models import CallOut
from call_outs import now_uk_naive, _webhook_content, _post_call_out, _call_out_link

JOB_NAME = "call_outs_check"
REMINDER_INTERVAL = timedelta(hours=24)


def main() -> None:
    now = now_uk_naive()
    print(f"Call-outs check — {now.strftime('%Y-%m-%d %H:%M')} (UK)")

    expired = 0
    reminded = 0
    errors: list[str] = []

    with Session(engine) as db:
        open_call_outs = db.exec(
            select(CallOut).where(CallOut.status == "open").order_by(CallOut.id)
        ).all()

        for c in open_call_outs:
            try:
                if c.game_at <= now:
                    c.status = "expired"
                    c.updated_at = now
                    db.add(c)
                    db.commit()
                    expired += 1
                    print(f"[call_out {c.id}] EXPIRED — game_at {c.game_at} has passed")
                    continue

                if c.last_reminder_at is None or (now - c.last_reminder_at) >= REMINDER_INTERVAL:
                    _post_call_out(
                        db, c.club_id, c.system,
                        _webhook_content(
                            c,
                            f"⏳ **Still looking for a game** — "
                            f"{name_with_mention(db, c.creator_name, c.creator_player_id)} has an open Call Out",
                            _call_out_link(db, c.club_id, c),
                        ),
                    )
                    c.last_reminder_at = now
                    c.updated_at = now
                    db.add(c)
                    db.commit()
                    reminded += 1
                    print(f"[call_out {c.id}] REMINDED — club={c.club_id} system={c.system}")
            except Exception as exc:
                import traceback
                db.rollback()
                print(f"[call_out {c.id}] ERROR — {exc}")
                traceback.print_exc()
                errors.append(f"call_out {c.id}: {exc}")

        record_job_run(
            db, JOB_NAME,
            status="error" if errors else "ok",
            detail="; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else "") if errors else None,
        )
        db.commit()

    print(f"Done — {expired} expired, {reminded} reminded, {len(errors)} error(s).")


if __name__ == "__main__":
    main()
