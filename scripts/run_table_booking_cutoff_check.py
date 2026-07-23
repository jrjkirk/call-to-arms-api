"""Table-booking cutoff check — invoked by its own GitHub Actions workflow
(hourly, same cadence as auto-pairings-check).

For each club running each system with table_booking_enabled=True and a
"cutoff" send_mode config, fires the venue email for the upcoming session's
target week once the configured cutoff day/time has arrived, using headcount
so far (pairings may not exist yet at cutoff time — see
table_booking.py::compute_table_booking's fallback formula).

No separate last-run tracking is needed: send_table_booking_notification's
allow_duplicate=False guard already checks TableBookingNotification for an
existing 'sent' row per (club, system, week), so re-running this hourly and
re-checking is safe — a week already sent is silently skipped.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from database import engine, record_job_run
from models import ClubSystem, SystemConfig, TableBookingConfig
from table_booking import send_table_booking_notification
from week_logic import _is_table_booking_cutoff_due, is_session_week, next_session_date

JOB_NAME = "table_booking_cutoff_check"


def main() -> None:
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    print(f"Table-booking cutoff check — {now_uk.strftime('%Y-%m-%d %H:%M %Z')}")

    today = now_uk.date()
    errors: list[str] = []

    with Session(engine) as db:
        system_configs = db.exec(
            select(SystemConfig).where(SystemConfig.active == True).order_by(SystemConfig.id)
        ).all()
        for system_config in system_configs:
            system = system_config.legacy_system_name
            try:
                club_systems = db.exec(
                    select(ClubSystem)
                    .where(ClubSystem.system_id == system_config.id)
                    .where(ClubSystem.enabled == True)
                    .where(ClubSystem.table_booking_enabled == True)
                ).all()
                if not club_systems:
                    continue

                for club_system in club_systems:
                    club_id = club_system.club_id
                    try:
                        cfg = db.exec(
                            select(TableBookingConfig)
                            .where(TableBookingConfig.club_id == club_id)
                            .where(TableBookingConfig.system_id == system_config.id)
                        ).first()
                        if cfg is None or cfg.send_mode != "cutoff":
                            continue
                        if not cfg.cutoff_day or not cfg.cutoff_time:
                            print(f"[{system} club={club_id}] SKIP — cutoff mode but no day/time configured")
                            continue

                        target_session_date = next_session_date(
                            club_system.session_day, club_system.session_cadence,
                            club_system.cadence_anchor, today,
                        )
                        if not is_session_week(
                            club_system.session_cadence, club_system.cadence_anchor,
                            target_session_date, today,
                        ):
                            continue

                        if not _is_table_booking_cutoff_due(cfg.cutoff_day, cfg.cutoff_time, now_uk):
                            continue

                        target_week = target_session_date.strftime("%d/%m/%Y")
                        notif = send_table_booking_notification(
                            db, club_id, system_config.id, system, target_week, cfg,
                            allow_duplicate=False,
                        )
                        if notif is None:
                            print(f"[{system} club={club_id}] SKIP — already sent for {target_week}")
                        else:
                            print(
                                f"[{system} club={club_id}] DONE — cutoff send for {target_week}, "
                                f"status={notif.status}, tables={notif.tables}, headcount={notif.headcount}"
                            )

                    except Exception as exc:
                        import traceback
                        print(f"[{system} club={club_id}] ERROR — {exc}")
                        traceback.print_exc()
                        errors.append(f"{system} club={club_id}: {exc}")

            except Exception as exc:
                import traceback
                print(f"[{system}] ERROR — {exc}")
                traceback.print_exc()
                errors.append(f"{system}: {exc}")

        record_job_run(
            db, JOB_NAME,
            status="error" if errors else "ok",
            detail="; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else "") if errors else None,
        )
        db.commit()


if __name__ == "__main__":
    main()
