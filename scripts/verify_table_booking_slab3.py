"""One-off verification for table-booking Slab 3 (cutoff-mode scheduled
send). Real staging DB, direct calls into
scripts/run_table_booking_cutoff_check.py's main(), in-memory temp
Player/Signup rows for whatever real target week TOW's actual ClubSystem
schedule resolves to today (the cutoff script always computes its own
target week from the schedule — unlike the manual endpoints, it doesn't
take a week parameter — so this test has to work with today's real target
week rather than a synthetic far-future one).

Checks:
  - _is_table_booking_cutoff_due unit cases (right day+time, wrong time,
    wrong day)
  - end-to-end: config with cutoff_day/time set to "right now" (UK) fires
    a real send via the actual script's main(), landing a real email
  - running main() again does NOT double-send (idempotency guard)

Run: PYTHONPATH=. python scripts/verify_table_booking_slab3.py <your-email>
"""
import importlib.util
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from sqlmodel import Session, select

from database import engine
from models import (
    ClubSystem, Player, Signup, SystemConfig, TableBookingConfig, TableBookingNotification,
)
from week_logic import _is_table_booking_cutoff_due, next_session_date

MANCHESTER_CLUB_ID = 1
TOW_SYSTEM = "The Old World"


def _load_cutoff_script():
    spec = importlib.util.spec_from_file_location(
        "run_table_booking_cutoff_check", "scripts/run_table_booking_cutoff_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, "scripts")
    spec.loader.exec_module(mod)
    return mod


def verify_due_check():
    now = datetime(2026, 3, 4, 17, 30, tzinfo=ZoneInfo("Europe/London"))  # a Wednesday
    assert _is_table_booking_cutoff_due("Wednesday", "17:00", now) is True
    assert _is_table_booking_cutoff_due("Wednesday", "19:00", now) is False  # outside 90min window
    assert _is_table_booking_cutoff_due("Thursday", "17:00", now) is False  # wrong day
    print("1. _is_table_booking_cutoff_due unit cases OK")


def main():
    if len(sys.argv) != 2:
        print("Usage: PYTHONPATH=. python scripts/verify_table_booking_slab3.py <your-email>")
        sys.exit(1)
    venue_email = sys.argv[1]

    verify_due_check()

    cutoff_mod = _load_cutoff_script()
    temp_player_ids: list[int] = []

    try:
        with Session(engine) as db:
            sc = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW_SYSTEM)).first()
            system_id = sc.id
            cs = db.exec(select(ClubSystem).where(
                ClubSystem.club_id == MANCHESTER_CLUB_ID, ClubSystem.system_id == system_id,
            )).first()

            now_uk = datetime.now(ZoneInfo("Europe/London"))
            target_date = next_session_date(cs.session_day, cs.session_cadence, cs.cadence_anchor, now_uk.date())
            target_week = target_date.strftime("%d/%m/%Y")
            print(f"2. TOW's real next session date resolves to {target_week} (day={cs.session_day})")

            # cutoff_day/time set to "right now" (UK) so the due-check fires immediately
            cutoff_day = now_uk.strftime("%A")
            cutoff_time = now_uk.strftime("%H:%M")

            cs.table_booking_enabled = True
            db.add(cs)
            cfg = TableBookingConfig(
                club_id=MANCHESTER_CLUB_ID, system_id=system_id,
                venue_name="EG NWGC (slab3 verify)", venue_email=venue_email,
                players_per_table=2, include_player_names=True, send_mode="cutoff",
                cutoff_day=cutoff_day, cutoff_time=cutoff_time,
                subject_template="[TEST] Call to Arms cutoff-mode verification",
            )
            db.add(cfg)

            # 2 temp players signed up for the real target week
            names = ["ZZTest Cutoff Frank", "ZZTest Cutoff Grace"]
            for n in names:
                p = Player(name=n, club_id=MANCHESTER_CLUB_ID)
                db.add(p)
                db.flush()
                temp_player_ids.append(p.id)
                db.add(Signup(
                    week=target_week, system=TOW_SYSTEM, player_id=p.id, player_name=p.name,
                    club_id=MANCHESTER_CLUB_ID,
                ))
            db.commit()
            print(f"3. config set to fire now (cutoff_day={cutoff_day}, cutoff_time={cutoff_time}), "
                  f"2 temp signups added for {target_week}")

        # --- run the real script's main() — should send exactly once ---
        cutoff_mod.main()
        with Session(engine) as db:
            sends = db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID,
                TableBookingNotification.system_id == system_id,
                TableBookingNotification.week == target_week,
            )).all()
            assert len(sends) == 1, f"expected 1 notification after first run, got {len(sends)}"
            assert sends[0].status == "sent", sends[0].error
            print(f"4. first main() run sent real email OK — tables={sends[0].tables}, "
                  f"headcount={sends[0].headcount} — check your inbox")

        # --- run again — must NOT double-send ---
        cutoff_mod.main()
        with Session(engine) as db:
            sends = db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID,
                TableBookingNotification.system_id == system_id,
                TableBookingNotification.week == target_week,
            )).all()
            assert len(sends) == 1, f"expected still 1 notification after second run, got {len(sends)}"
            print("5. second main() run did NOT double-send (idempotency guard) OK")

        print("\nAll checks passed. One real cutoff-mode test email should have arrived at", venue_email)

    finally:
        with Session(engine) as db:
            sc = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW_SYSTEM)).first()
            for n in db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID, TableBookingNotification.system_id == sc.id,
            )).all():
                db.delete(n)
            for s in db.exec(select(Signup).where(
                Signup.club_id == MANCHESTER_CLUB_ID, Signup.player_name.like("ZZTest Cutoff%"),
            )).all():
                db.delete(s)
            cfg = db.exec(select(TableBookingConfig).where(
                TableBookingConfig.club_id == MANCHESTER_CLUB_ID, TableBookingConfig.system_id == sc.id,
            )).first()
            if cfg:
                db.delete(cfg)
            cs = db.exec(select(ClubSystem).where(
                ClubSystem.club_id == MANCHESTER_CLUB_ID, ClubSystem.system_id == sc.id,
            )).first()
            if cs:
                cs.table_booking_enabled = False
                db.add(cs)
            db.commit()
            for pid in temp_player_ids:
                p = db.get(Player, pid)
                if p:
                    db.delete(p)
            db.commit()
        print("Cleanup done: temp signups/players/config/notifications removed, "
              "table_booking_enabled reset to False.")


if __name__ == "__main__":
    main()
