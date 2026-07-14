"""One-off verification script for the app_settings/club_settings split —
last piece of Phase 1's 10-table club_id rollout. Not part of the app; run
manually against staging.

Exercises GET/POST /admin/auto-pairings-settings for all three systems
through FastAPI TestClient, against the real staging DB, with only the auth
dependency overridden (in-memory fake super-admin, no real User row
touched). Also directly exercises run_auto_pairings_check.py's
_get_setting/_upsert_setting helpers (same reasoning as the publish_state
handoff: running the real scheduler script is invasive since it also
generates/publishes pairings and posts to Discord — verifying the identical
construction/query code directly is the substitute).

Confirms systems_from_catalogue (the one genuinely global app_settings row)
is untouched throughout.

All rows this script writes to club_settings are cleaned up in a `finally`,
so staging is left exactly as it started.

Run with: python verify_club_settings.py [--post-contract]
--post-contract is a no-op here (club_settings has no nullable/contract
step — the column is NOT NULL from creation) but kept for symmetry with the
other tables' verify scripts.
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select, text

from database import engine, _default_club_id
from models import AppSetting, ClubSetting, Club, User
from main import app
import auth

SYSTEMS = ["The Old World", "The Horus Heresy", "Kill Team"]


def _slug(system: str) -> str:
    return system.replace(" ", "").replace("'", "")


def _fake_super_admin(uid: int = 999998) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-club-settings-{uid}",
        discord_name="Verify Script",
        player_id=None,
        is_super_admin=True,
    )


def main():
    with Session(engine) as db:
        manchester_id = _default_club_id(db)
    print(f"Manchester club_id = {manchester_id}")

    client = TestClient(app)
    fake_admin = _fake_super_admin()
    app.dependency_overrides[auth.require_user] = lambda: fake_admin
    app.dependency_overrides[auth.current_user] = lambda: fake_admin

    problems = []

    try:
        with Session(engine) as db:
            before = db.exec(text("SELECT value FROM app_settings WHERE key = 'systems_from_catalogue'")).first()
        print(f"systems_from_catalogue before test: {before}")

        for system in SYSTEMS:
            slug = _slug(system)

            resp = client.post("/admin/auto-pairings-settings", json={
                "system": system,
                "enabled": True,
                "day": "Wednesday",
                "time": "21:00",
            })
            if resp.status_code != 200:
                problems.append(f"[{system}] POST failed: {resp.status_code} {resp.text}")
                continue

            with Session(engine) as db:
                row = db.get(ClubSetting, (manchester_id, f"auto_pairings_{slug}_enabled"))
                if row is None or row.value != "true":
                    problems.append(f"[{system}] club_settings row for enabled missing/wrong: {row}")
                day_row = db.get(ClubSetting, (manchester_id, f"auto_pairings_{slug}_day"))
                if day_row is None or day_row.value != "Wednesday":
                    problems.append(f"[{system}] club_settings row for day missing/wrong: {day_row}")
                time_row = db.get(ClubSetting, (manchester_id, f"auto_pairings_{slug}_time"))
                if time_row is None or time_row.value != "21:00":
                    problems.append(f"[{system}] club_settings row for time missing/wrong: {time_row}")

            get_resp = client.get("/admin/auto-pairings-settings", params={"system": system})
            if get_resp.status_code != 200:
                problems.append(f"[{system}] GET failed: {get_resp.status_code} {get_resp.text}")
                continue
            body = get_resp.json()
            if body["enabled"] is not True or body["day"] != "Wednesday" or body["time"] != "21:00":
                problems.append(f"[{system}] GET returned unexpected body: {body}")
            print(f"[{system}] GET /admin/auto-pairings-settings -> {body}")

            # Directly exercise run_auto_pairings_check.py's helper pair,
            # same construction/query code the scheduler uses.
            import run_auto_pairings_check as rapc
            with Session(engine) as db:
                scheduler_enabled = rapc._get_setting(db, f"auto_pairings_{slug}_enabled", "false")
                if scheduler_enabled != "true":
                    problems.append(f"[{system}] scheduler _get_setting mismatch: {scheduler_enabled}")
                rapc._upsert_setting(db, f"auto_pairings_{slug}_last_week", "TESTWEEK")
                db.commit()
                lw_row = db.get(ClubSetting, (manchester_id, f"auto_pairings_{slug}_last_week"))
                if lw_row is None or lw_row.value != "TESTWEEK":
                    problems.append(f"[{system}] scheduler _upsert_setting mismatch: {lw_row}")

        with Session(engine) as db:
            after = db.exec(text("SELECT value FROM app_settings WHERE key = 'systems_from_catalogue'")).first()
        if before != after:
            problems.append(f"systems_from_catalogue changed! before={before} after={after}")
        print(f"systems_from_catalogue after test: {after}")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for system in SYSTEMS:
                slug = _slug(system)
                for suffix in ("enabled", "day", "time", "last_week"):
                    row = db.get(ClubSetting, (manchester_id, f"auto_pairings_{slug}_{suffix}"))
                    if row is not None:
                        db.delete(row)
            db.commit()
            remaining = db.exec(select(ClubSetting)).all()
            print(f"club_settings rows remaining after cleanup: {len(remaining)} (expect 0)")

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: club_settings read/write works for all 3 systems, systems_from_catalogue untouched.")


if __name__ == "__main__":
    main()
