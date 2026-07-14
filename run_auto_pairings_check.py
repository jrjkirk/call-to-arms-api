"""Hourly auto-pairings check — invoked by the auto-pairings-check GitHub Actions workflow.

For each system, reads auto-pairings settings from app_settings, decides
whether pairings are due, generates + publishes them if so, and posts the
image to Discord. One system failing does not stop the others.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session

from database import engine, _default_club_id, scoped
from models import ClubSetting, Pairing, PublishState, Signup
from pairings_engine import generate
from post_pairings_image import post_pairings_image_for
from run_hh_call_to_arms import is_hh_session_week
from week_logic import _is_auto_pairings_due, week_id_for_system

SYSTEMS = ["The Old World", "The Horus Heresy", "Kill Team"]


def _slug(system: str) -> str:
    return system.replace(" ", "").replace("'", "")


def _get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    club_id = _default_club_id(db)
    row = db.get(ClubSetting, (club_id, key))
    return row.value if row is not None else default


def _upsert_setting(db: Session, key: str, value: str) -> None:
    club_id = _default_club_id(db)
    row = db.get(ClubSetting, (club_id, key))
    if row is None:
        row = ClubSetting(club_id=club_id, key=key, value=value)
    else:
        row.value = value
    db.add(row)


def main() -> None:
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    print(f"Auto-pairings check — {now_uk.strftime('%Y-%m-%d %H:%M %Z')}")

    with Session(engine) as db:
        for system in SYSTEMS:
            try:
                slug = _slug(system)

                settings = {
                    "enabled": (
                        _get_setting(db, f"auto_pairings_{slug}_enabled", "false") or "false"
                    ).lower() == "true",
                    "day": _get_setting(db, f"auto_pairings_{slug}_day", "Tuesday") or "Tuesday",
                    "time": _get_setting(db, f"auto_pairings_{slug}_time", "20:00") or "20:00",
                    "last_week": _get_setting(db, f"auto_pairings_{slug}_last_week", None),
                }

                target_week = week_id_for_system(system, now_uk.date())

                if system == "The Horus Heresy" and not is_hh_session_week(now_uk.date()):
                    print(f"[{system}] SKIP — not an HH session week")
                    continue

                if not _is_auto_pairings_due(settings, now_uk, target_week):
                    print(
                        f"[{system}] SKIP — not due "
                        f"(enabled={settings['enabled']}, day={settings['day']}, "
                        f"time={settings['time']}, last_week={settings['last_week']!r}, "
                        f"target={target_week})"
                    )
                    continue

                club_id = _default_club_id(db)

                signups = db.exec(
                    scoped(Signup, club_id)
                    .where(Signup.system == system)
                    .where(Signup.week == target_week)
                ).all()

                if not signups:
                    print(f"[{system}] SKIP — no signups for {target_week}; recording last_week to avoid retrying")
                    _upsert_setting(db, f"auto_pairings_{slug}_last_week", target_week)
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

                _upsert_setting(db, f"auto_pairings_{slug}_last_week", target_week)
                db.commit()

                posted = post_pairings_image_for(db, system, target_week)
                print(f"[{system}] DONE — pairings generated+published for {target_week}, image_posted={posted}")

            except Exception as exc:
                import traceback
                print(f"[{system}] ERROR — {exc}")
                traceback.print_exc()


if __name__ == "__main__":
    main()
