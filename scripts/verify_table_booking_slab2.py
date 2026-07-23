"""One-off verification for table-booking Slab 2 (table math + preview/send +
on_publish auto-send + idempotency). Real staging DB, FastAPI TestClient,
in-memory fake super-admin linked to Manchester (club_id=1). Uses temporary
"ZZTest" Player/Signup/Pairing rows for a far-future test week (01/01/2099)
so real data is never touched, and sends real test emails via Resend to
prove the on_publish/manual-send paths work end-to-end (not just emailer.py
in isolation, which Slab 0 already proved).

Checks:
  - preview 404s before any config exists
  - preview falls back to ceil(headcount/players_per_table) with no pairings
  - preview reflects real pairing-based table count once pairings exist
  - POST /admin/pairings/publish auto-sends via maybe_send_table_booking
    (on_publish mode) — one real email sent, one 'sent' notification row
  - publishing again does NOT send a second email (idempotency guard)
  - GET /admin/table-booking-history shows the send
  - POST /admin/table-booking/send (manual) DOES send again (allow_duplicate)

Run: PYTHONPATH=. python scripts/verify_table_booking_slab2.py <your-email>
"""
import sys

from dotenv import load_dotenv

load_dotenv()

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import auth
from database import engine
from main import app
from models import (
    ClubSystem, Pairing, Player, PublishState, Signup, SystemConfig,
    TableBookingConfig, TableBookingNotification, User,
)

MANCHESTER_CLUB_ID = 1
TOW_SYSTEM = "The Old World"
TEST_WEEK = "01/01/2099"


def fake_user():
    return User(
        id=999999, discord_id="fake", discord_name="Verify Script",
        is_super_admin=True, club_id=MANCHESTER_CLUB_ID,
    )


def main():
    if len(sys.argv) != 2:
        print("Usage: PYTHONPATH=. python scripts/verify_table_booking_slab2.py <your-email>")
        sys.exit(1)
    venue_email = sys.argv[1]

    app.dependency_overrides[auth.require_user] = fake_user
    client = TestClient(app)

    temp_player_ids: list[int] = []

    try:
        with Session(engine) as db:
            sc = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW_SYSTEM)).first()
            system_id = sc.id

            # --- 1. preview 404s before config exists ---
            r = client.post("/admin/table-booking/preview", json={"system": TOW_SYSTEM, "week": TEST_WEEK})
            assert r.status_code == 404, r.text
            print("1. preview without config -> 404 OK")

            # --- set up config: enabled, on_publish, real venue_email for real send ---
            cs = db.exec(select(ClubSystem).where(
                ClubSystem.club_id == MANCHESTER_CLUB_ID, ClubSystem.system_id == system_id,
            )).first()
            cs.table_booking_enabled = True
            db.add(cs)
            cfg = TableBookingConfig(
                club_id=MANCHESTER_CLUB_ID, system_id=system_id,
                venue_name="EG NWGC (verify script)", venue_email=venue_email,
                players_per_table=2, include_player_names=True, send_mode="on_publish",
                subject_template="[TEST] Call to Arms table-booking verification",
            )
            db.add(cfg)
            db.commit()
            print("2. config created (enabled, on_publish, players_per_table=2)")

            # --- create 3 temp players + signups (odd number -> 1 BYE expected) ---
            names = ["ZZTest TB Alice", "ZZTest TB Bob", "ZZTest TB Carol"]
            players = []
            for n in names:
                p = Player(name=n, club_id=MANCHESTER_CLUB_ID)
                db.add(p)
                db.flush()
                temp_player_ids.append(p.id)
                players.append(p)
            for p in players:
                db.add(Signup(
                    week=TEST_WEEK, system=TOW_SYSTEM, player_id=p.id, player_name=p.name,
                    club_id=MANCHESTER_CLUB_ID,
                ))
            db.commit()
            print(f"3. created {len(players)} temp players + signups for {TEST_WEEK}")

        # --- 4. preview with signups but no pairings: fallback formula ---
        r = client.post("/admin/table-booking/preview", json={"system": TOW_SYSTEM, "week": TEST_WEEK})
        assert r.status_code == 200, r.text
        preview = r.json()
        assert preview["headcount"] == 3, preview
        assert preview["tables"] == 2, preview  # ceil(3/2) = 2
        assert preview["already_sent"] is False, preview
        assert "ZZTest TB Alice" in preview["player_names"], preview
        print("4. preview fallback formula (no pairings) OK:", preview["tables"], preview["headcount"])

        # --- create real pairings: 1 real match + 1 BYE (matches 3 players) ---
        with Session(engine) as db:
            signups = db.exec(select(Signup).where(
                Signup.club_id == MANCHESTER_CLUB_ID, Signup.week == TEST_WEEK, Signup.system == TOW_SYSTEM,
            )).all()
            assert len(signups) == 3
            db.add(Pairing(
                week=TEST_WEEK, system=TOW_SYSTEM, club_id=MANCHESTER_CLUB_ID,
                a_signup_id=signups[0].id, b_signup_id=signups[1].id,
            ))
            db.add(Pairing(
                week=TEST_WEEK, system=TOW_SYSTEM, club_id=MANCHESTER_CLUB_ID,
                a_signup_id=signups[2].id, b_signup_id=None,  # BYE
            ))
            db.commit()
        print("5. created 1 real pairing + 1 BYE pairing")

        # --- 6. preview now reflects pairing-based table count (1 table, not 2) ---
        r = client.post("/admin/table-booking/preview", json={"system": TOW_SYSTEM, "week": TEST_WEEK})
        preview2 = r.json()
        assert preview2["tables"] == 1, preview2  # only the real match needs a table
        assert preview2["headcount"] == 3, preview2
        print("6. preview reflects pairing-based table count OK:", preview2["tables"])

        # --- 7. publish -> auto-sends real email via on_publish trigger ---
        r = client.post("/admin/pairings/publish", json={"system": TOW_SYSTEM, "week": TEST_WEEK, "published": True})
        assert r.status_code == 200, r.text
        with Session(engine) as db:
            sends = db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID,
                TableBookingNotification.system_id == system_id,
                TableBookingNotification.week == TEST_WEEK,
            )).all()
            assert len(sends) == 1, f"expected 1 notification after first publish, got {len(sends)}"
            assert sends[0].status == "sent", sends[0].error
            assert sends[0].tables == 1 and sends[0].headcount == 3
        print("7. publish auto-sent real email OK (1 notification row, status=sent) — check your inbox")

        # --- 8. publishing again does NOT send a second email (idempotent) ---
        r = client.post("/admin/pairings/publish", json={"system": TOW_SYSTEM, "week": TEST_WEEK, "published": True})
        assert r.status_code == 200, r.text
        with Session(engine) as db:
            sends = db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID,
                TableBookingNotification.system_id == system_id,
                TableBookingNotification.week == TEST_WEEK,
            )).all()
            assert len(sends) == 1, f"expected still 1 notification after re-publish, got {len(sends)}"
        print("8. re-publish did NOT double-send (idempotency guard) OK")

        # --- 9. history shows the send ---
        r = client.get("/admin/table-booking-history", params={"system": TOW_SYSTEM})
        history = r.json()
        assert len(history) >= 1 and history[0]["week"] == TEST_WEEK and history[0]["status"] == "sent"
        print("9. history endpoint reflects the send OK")

        # --- 10. manual send bypasses idempotency (allow_duplicate=True) ---
        r = client.post("/admin/table-booking/send", json={"system": TOW_SYSTEM, "week": TEST_WEEK})
        assert r.status_code == 200, r.text
        manual = r.json()
        assert manual["status"] == "sent", manual
        with Session(engine) as db:
            sends = db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID,
                TableBookingNotification.system_id == system_id,
                TableBookingNotification.week == TEST_WEEK,
            )).all()
            assert len(sends) == 2, f"expected 2 notifications after manual send, got {len(sends)}"
        print("10. manual send bypassed idempotency, sent again OK (2 notification rows) — check your inbox again")

        print("\nAll checks passed. Two real test emails should have arrived at", venue_email)

    finally:
        with Session(engine) as db:
            sc = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW_SYSTEM)).first()
            # notifications
            for n in db.exec(select(TableBookingNotification).where(
                TableBookingNotification.club_id == MANCHESTER_CLUB_ID,
                TableBookingNotification.system_id == sc.id,
                TableBookingNotification.week == TEST_WEEK,
            )).all():
                db.delete(n)
            # pairings
            for p in db.exec(select(Pairing).where(
                Pairing.club_id == MANCHESTER_CLUB_ID, Pairing.week == TEST_WEEK, Pairing.system == TOW_SYSTEM,
            )).all():
                db.delete(p)
            # publish_state
            gate = db.exec(select(PublishState).where(
                PublishState.club_id == MANCHESTER_CLUB_ID, PublishState.week == TEST_WEEK, PublishState.system == TOW_SYSTEM,
            )).first()
            if gate:
                db.delete(gate)
            # signups
            for s in db.exec(select(Signup).where(
                Signup.club_id == MANCHESTER_CLUB_ID, Signup.week == TEST_WEEK, Signup.system == TOW_SYSTEM,
            )).all():
                db.delete(s)
            # config + enabled flag
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
            # temp players (separate pass, no FK from signups left referencing them)
            for pid in temp_player_ids:
                p = db.get(Player, pid)
                if p:
                    db.delete(p)
            db.commit()
        print("Cleanup done: all temp signups/pairings/players/config/notifications removed, "
              "table_booking_enabled reset to False.")


if __name__ == "__main__":
    main()
