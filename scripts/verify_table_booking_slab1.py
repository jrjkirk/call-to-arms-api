"""One-off verification for table-booking Slab 1 (data model + admin config
CRUD, no sending yet). Real staging DB, FastAPI TestClient, only
auth.require_user overridden with an in-memory fake super-admin linked to
Manchester (club_id=1) — no real User row touched.

Checks:
  - GET /admin/table-booking-settings returns the unsaved-defaults shape
    before any config exists
  - POST /admin/table-booking-settings toggles ClubSystem.table_booking_enabled
  - POST /admin/table-booking-config validation: bad send_mode, bad email,
    players_per_table < 1, cutoff mode missing day/time all 422
  - POST /admin/table-booking-config upserts correctly, GET reflects it
  - a second save updates the same row (upsert, not duplicate)

Run: PYTHONPATH=. python scripts/verify_table_booking_slab1.py
"""
from dotenv import load_dotenv

load_dotenv()

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import auth
from database import engine
from main import app
from models import TableBookingConfig, User

MANCHESTER_CLUB_ID = 1
TOW_SYSTEM = "The Old World"


def fake_user():
    return User(
        id=999999, discord_id="fake", discord_name="Verify Script",
        is_super_admin=True, club_id=MANCHESTER_CLUB_ID,
    )


def main():
    app.dependency_overrides[auth.require_user] = fake_user
    client = TestClient(app)

    try:
        # 1. defaults before any config exists
        r = client.get("/admin/table-booking-settings", params={"system": TOW_SYSTEM})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["table_booking_enabled"] is False, body
        assert body["venue_email"] == "", body
        assert body["send_mode"] == "on_publish", body
        print("1. defaults OK:", body)

        # 2. toggle enabled flag
        r = client.post("/admin/table-booking-settings", json={
            "system": TOW_SYSTEM, "table_booking_enabled": True,
        })
        assert r.status_code == 200, r.text
        r = client.get("/admin/table-booking-settings", params={"system": TOW_SYSTEM})
        assert r.json()["table_booking_enabled"] is True
        print("2. toggle OK")

        # 3. validation: bad send_mode
        r = client.post("/admin/table-booking-config", json={
            "system": TOW_SYSTEM, "venue_email": "venue@example.com", "send_mode": "bogus",
        })
        assert r.status_code == 422, r.text
        print("3a. bad send_mode -> 422 OK")

        # bad email
        r = client.post("/admin/table-booking-config", json={
            "system": TOW_SYSTEM, "venue_email": "not-an-email",
        })
        assert r.status_code == 422, r.text
        print("3b. bad email -> 422 OK")

        # players_per_table < 1
        r = client.post("/admin/table-booking-config", json={
            "system": TOW_SYSTEM, "venue_email": "venue@example.com", "players_per_table": 0,
        })
        assert r.status_code == 422, r.text
        print("3c. players_per_table=0 -> 422 OK")

        # cutoff mode missing day/time
        r = client.post("/admin/table-booking-config", json={
            "system": TOW_SYSTEM, "venue_email": "venue@example.com", "send_mode": "cutoff",
        })
        assert r.status_code == 422, r.text
        print("3d. cutoff missing day/time -> 422 OK")

        # 4. real upsert (create)
        r = client.post("/admin/table-booking-config", json={
            "system": TOW_SYSTEM,
            "venue_name": "EG NWGC (test)",
            "venue_email": "venue-test@example.com",
            "cc_emails": ["cc-test@example.com"],
            "players_per_table": 2,
            "include_player_names": True,
            "send_mode": "on_publish",
        })
        assert r.status_code == 200, r.text
        cfg = r.json()
        assert cfg["venue_name"] == "EG NWGC (test)"
        assert cfg["venue_email"] == "venue-test@example.com"
        assert cfg["cc_emails"] == ["cc-test@example.com"]
        print("4. create OK:", cfg)

        # confirm exactly one row exists (upsert, not append)
        with Session(engine) as db:
            rows = db.exec(select(TableBookingConfig).where(
                TableBookingConfig.club_id == MANCHESTER_CLUB_ID,
            )).all()
            assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
            row_id = rows[0].id
        print("4b. exactly 1 row in DB, id =", row_id)

        # 5. second save updates the same row
        r = client.post("/admin/table-booking-config", json={
            "system": TOW_SYSTEM,
            "venue_name": "EG NWGC (test, renamed)",
            "venue_email": "venue-test2@example.com",
            "send_mode": "cutoff",
            "cutoff_day": "Wednesday",
            "cutoff_time": "17:00",
        })
        assert r.status_code == 200, r.text
        cfg2 = r.json()
        assert cfg2["venue_name"] == "EG NWGC (test, renamed)"
        assert cfg2["send_mode"] == "cutoff"
        assert cfg2["cutoff_day"] == "Wednesday"
        assert cfg2["cutoff_time"] == "17:00"
        with Session(engine) as db:
            rows = db.exec(select(TableBookingConfig).where(
                TableBookingConfig.club_id == MANCHESTER_CLUB_ID,
            )).all()
            assert len(rows) == 1, f"expected still 1 row after update, got {len(rows)}"
            assert rows[0].id == row_id, "second save created a new row instead of updating"
        print("5. update-in-place OK (same row id, new values)")

        # 6. GET reflects latest state
        r = client.get("/admin/table-booking-settings", params={"system": TOW_SYSTEM})
        final = r.json()
        assert final["send_mode"] == "cutoff"
        assert final["table_booking_enabled"] is True
        print("6. GET reflects latest state OK")

        print("\nAll checks passed.")

    finally:
        # cleanup: restore disabled flag + delete the test config row
        with Session(engine) as db:
            from models import ClubSystem, SystemConfig
            sc = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW_SYSTEM)).first()
            cs = db.exec(select(ClubSystem).where(
                ClubSystem.club_id == MANCHESTER_CLUB_ID, ClubSystem.system_id == sc.id,
            )).first()
            if cs:
                cs.table_booking_enabled = False
                db.add(cs)
            cfg = db.exec(select(TableBookingConfig).where(
                TableBookingConfig.club_id == MANCHESTER_CLUB_ID, TableBookingConfig.system_id == sc.id,
            )).first()
            if cfg:
                db.delete(cfg)
            db.commit()
        print("Cleanup done: table_booking_enabled reset to False, test config row deleted.")


if __name__ == "__main__":
    main()
