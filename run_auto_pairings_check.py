"""Hourly auto-pairings check — invoked by the auto-pairings-check GitHub Actions workflow.

For each club running each system (per club_systems), reads that club's
auto-pairings settings, decides whether pairings are due, generates +
publishes them if so, and posts the image to Discord. One club/system
failing does not stop the others.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from database import engine, scoped
from models import ClubSetting, ClubSystem, Pairing, PublishState, Signup, SystemConfig
from pairings_engine import generate
from post_pairings_image import post_pairings_image_for
from week_logic import _is_auto_pairings_due, is_session_week, next_session_date

SYSTEMS = ["The Old World", "The Horus Heresy", "Kill Team"]


def _slug(system: str) -> str:
    return system.replace(" ", "").replace("'", "")


def _get_setting(db: Session, club_id: int, key: str, default: str | None = None) -> str | None:
    row = db.get(ClubSetting, (club_id, key))
    return row.value if row is not None else default


def _upsert_setting(db: Session, club_id: int, key: str, value: str) -> None:
    row = db.get(ClubSetting, (club_id, key))
    if row is None:
        row = ClubSetting(club_id=club_id, key=key, value=value)
    else:
        row.value = value
    db.add(row)


def main() -> None:
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    print(f"Auto-pairings check — {now_uk.strftime('%Y-%m-%d %H:%M %Z')}")

    today = now_uk.date()

    with Session(engine) as db:
        for system in SYSTEMS:
            try:
                slug = _slug(system)

                system_config = db.exec(
                    select(SystemConfig).where(SystemConfig.legacy_system_name == system)
                ).first()
                if system_config is None:
                    print(f"[{system}] ERROR — no SystemConfig row for legacy_system_name={system!r}")
                    continue

                club_systems = db.exec(
                    select(ClubSystem).where(ClubSystem.system_id == system_config.id)
                ).all()
                if not club_systems:
                    print(f"[{system}] SKIP — no club_systems rows for this system")
                    continue

                for club_system in club_systems:
                    club_id = club_system.club_id
                    try:
                        target_week_date = next_session_date(
                            club_system.session_day, club_system.session_cadence,
                            club_system.cadence_anchor, today,
                        )
                        target_week = target_week_date.strftime("%d/%m/%Y")

                        if not is_session_week(
                            club_system.session_cadence, club_system.cadence_anchor,
                            target_week_date, today,
                        ):
                            print(
                                f"[{system} club={club_id}] SKIP — not a session week "
                                f"(cadence={club_system.session_cadence})"
                            )
                            continue

                        settings = {
                            "enabled": (
                                _get_setting(db, club_id, f"auto_pairings_{slug}_enabled", "false") or "false"
                            ).lower() == "true",
                            "day": _get_setting(db, club_id, f"auto_pairings_{slug}_day", "Tuesday") or "Tuesday",
                            "time": _get_setting(db, club_id, f"auto_pairings_{slug}_time", "20:00") or "20:00",
                            "last_week": _get_setting(db, club_id, f"auto_pairings_{slug}_last_week", None),
                        }

                        if not _is_auto_pairings_due(settings, now_uk, target_week):
                            print(
                                f"[{system} club={club_id}] SKIP — not due "
                                f"(enabled={settings['enabled']}, day={settings['day']}, "
                                f"time={settings['time']}, last_week={settings['last_week']!r}, "
                                f"target={target_week})"
                            )
                            continue

                        signups = db.exec(
                            scoped(Signup, club_id)
                            .where(Signup.system == system)
                            .where(Signup.week == target_week)
                        ).all()

                        if not signups:
                            print(f"[{system} club={club_id}] SKIP — no signups for {target_week}; recording last_week to avoid retrying")
                            _upsert_setting(db, club_id, f"auto_pairings_{slug}_last_week", target_week)
                            db.commit()
                            continue

                        # Delete existing pending non-prearranged pairings before regenerating
                        old = db.exec(
                            scoped(Pairing, club_id)
                            .where(Pairing.system == system)
                            .where(Pairing.week == target_week)
                            .where(Pairing.status == "pending")
                            .where(Pairing.prearranged != True)
                        ).all()
                        for p in old:
                            db.delete(p)

                        generate(
                            db, target_week, system, allow_repeats_when_needed=True, persist=True,
                            club_id=club_id,
                        )

                        gate = db.exec(
                            scoped(PublishState, club_id)
                            .where(PublishState.system == system)
                            .where(PublishState.week == target_week)
                        ).first()
                        if gate is None:
                            gate = PublishState(
                                system=system,
                                week=target_week,
                                published=True,
                                club_id=club_id,
                            )
                        else:
                            gate.published = True
                        db.add(gate)

                        _upsert_setting(db, club_id, f"auto_pairings_{slug}_last_week", target_week)
                        db.commit()

                        posted = post_pairings_image_for(db, system, target_week, club_id=club_id)
                        print(f"[{system} club={club_id}] DONE — pairings generated+published for {target_week}, image_posted={posted}")

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
