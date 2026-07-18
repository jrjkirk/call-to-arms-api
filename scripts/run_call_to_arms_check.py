"""Hourly call-to-arms check — invoked by the call-to-arms-check GitHub Actions workflow.

For each club running each system (per club_systems), reads that club's
call-to-arms schedule settings, and if a post is due — N days before the
club's session day, at the configured time, not already posted this cycle —
posts the system's call-to-arms message to that club's configured
`call_to_arms` Discord webhook (from club_webhooks). One club/system
failing does not stop the others.

Replaces the three fixed-cron, single-club, non-club-aware scripts
(run_call_to_arms.py / run_hh_call_to_arms.py / run_kt_call_to_arms.py,
deleted) and their manual-fallback workflows. This workflow's own
workflow_dispatch trigger is the manual/emergency posting path now — still
club-aware, still resolved via club_webhooks, so it can't post to the
wrong club's channel.
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

import call_to_arms_content as cta_content
from database import engine, resolve_webhook_url, system_setting_slug as _slug, get_setting as _get_setting, upsert_setting as _upsert_setting
from models import ClubSystem, Mission, SystemConfig
from week_logic import _is_call_to_arms_due, is_session_week, next_session_date

APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")


def main() -> None:
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    print(f"Call-to-arms check — {now_uk.strftime('%Y-%m-%d %H:%M %Z')}")
    today = now_uk.date()

    with Session(engine) as db:
        # Iterate the active catalogue directly rather than a hardcoded list,
        # so a newly-added system is picked up automatically. Ordered by id
        # for a stable, deterministic run order.
        system_configs = db.exec(
            select(SystemConfig)
            .where(SystemConfig.active == True)
            .order_by(SystemConfig.id)
        ).all()
        for system_config in system_configs:
            system = system_config.legacy_system_name
            try:
                slug = _slug(system)

                club_systems = db.exec(
                    select(ClubSystem)
                    .where(ClubSystem.system_id == system_config.id)
                    .where(ClubSystem.enabled == True)
                ).all()
                if not club_systems:
                    print(f"[{system}] SKIP — no enabled club_systems rows")
                    continue

                for club_system in club_systems:
                    club_id = club_system.club_id
                    try:
                        next_session = next_session_date(
                            club_system.session_day, club_system.session_cadence,
                            club_system.cadence_anchor, today,
                        )
                        if not is_session_week(
                            club_system.session_cadence, club_system.cadence_anchor,
                            next_session, today,
                        ):
                            print(f"[{system} club={club_id}] SKIP — not a session week")
                            continue

                        target_week = next_session.strftime("%d/%m/%Y")
                        days_before = int(
                            _get_setting(db, club_id, f"call_to_arms_{slug}_days_before", "3") or "3"
                        )
                        settings = {
                            "enabled": (
                                _get_setting(db, club_id, f"call_to_arms_{slug}_enabled", "false") or "false"
                            ).lower() == "true",
                            "time": _get_setting(db, club_id, f"call_to_arms_{slug}_time", "12:00") or "12:00",
                            "last_week": _get_setting(db, club_id, f"call_to_arms_{slug}_last_week"),
                        }
                        post_date = next_session - timedelta(days=days_before)

                        if not _is_call_to_arms_due(settings, now_uk, target_week, post_date):
                            print(
                                f"[{system} club={club_id}] SKIP — not due "
                                f"(enabled={settings['enabled']}, days_before={days_before}, "
                                f"time={settings['time']}, post_date={post_date}, "
                                f"last_week={settings['last_week']!r}, target={target_week})"
                            )
                            continue

                        webhook_url = resolve_webhook_url(db, club_id, "call_to_arms", system_config.id)
                        if not webhook_url:
                            print(
                                f"[{system} club={club_id}] SKIP — due but no call_to_arms "
                                f"webhook configured for this club/system (set it in the admin Webhooks panel)"
                            )
                            continue

                        template = (
                            _get_setting(db, club_id, f"call_to_arms_{slug}_template")
                            or cta_content.default_template(system)
                        )
                        image_mode, image_url = cta_content.parse_image_setting(
                            _get_setting(db, club_id, f"call_to_arms_{slug}_image")
                        )
                        # Mission pool: when this club-system has missions
                        # enabled, pass its active DB missions (post picks one
                        # at random). Otherwise pass None so post() keeps the
                        # hardcoded SCENARIO_DATA fallback (Manchester TOW until
                        # migrated).
                        missions = None
                        if club_system.missions_enabled:
                            missions = [
                                {"name": m.name, "secondary_objectives": m.secondary_objectives,
                                 "image_url": m.image_url}
                                for m in db.exec(
                                    select(Mission)
                                    .where(Mission.club_id == club_id)
                                    .where(Mission.system_id == system_config.id)
                                    .where(Mission.active == True)
                                ).all()
                            ]
                        cta_content.post(
                            webhook_url, template, system, next_session, APP_PUBLIC_URL,
                            image_mode=image_mode, image_url=image_url, missions=missions,
                        )
                        _upsert_setting(db, club_id, f"call_to_arms_{slug}_last_week", target_week)
                        db.commit()
                        print(f"[{system} club={club_id}] DONE — posted call-to-arms for session {target_week}")

                    except Exception as exc:
                        import traceback
                        print(f"[{system} club={club_id}] ERROR — {exc}")
                        traceback.print_exc()

            except Exception as exc:
                import traceback
                print(f"[{system}] ERROR — {exc}")
                traceback.print_exc()


if __name__ == "__main__":
    main()
