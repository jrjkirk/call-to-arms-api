"""Seed Manchester's The Old World mission pool from the hardcoded
TOW_SCENARIOS, uploading each terrain image to Supabase Storage.

After this runs, Manchester + TOW has a DB mission pool matching today's
hardcoded rotation (including the image-less "Open Battle"), and its
ClubSystem is flipped to missions_enabled=True (+ missions_use_secondary,
since TOW missions carry secondary objectives). The hardcoded SCENARIO_DATA
fallback stays in place as a safety net.

Run against whichever environment the current .env / secrets point at — so
run once with staging creds, and again (later) with production creds:

    PYTHONPATH=. python seed/seed_manchester_tow_missions.py
    PYTHONPATH=. python seed/seed_manchester_tow_missions.py --verify-only
    PYTHONPATH=. python seed/seed_manchester_tow_missions.py --force   # reseed

Requires SUPABASE_URL / SUPABASE_SERVICE_KEY (storage) and DATABASE_URL. NOT
idempotent by default: refuses to run if Manchester TOW already has missions,
unless --force (which deletes the existing pool + its images first).
"""
import os
import sys

import httpx
from dotenv import load_dotenv
from sqlmodel import Session, select

load_dotenv()

import storage
from call_to_arms_content import BASE_DIR, TOW_SCENARIOS
from database import engine
from models import Club, ClubSystem, Mission, SystemConfig

TOW = "The Old World"


def _resolve(db: Session):
    club = db.exec(select(Club).where(Club.slug == "manchester")).first()
    if club is None:
        raise RuntimeError("No Manchester club (slug='manchester').")
    tow = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == TOW)).first()
    if tow is None:
        raise RuntimeError("No The Old World system in the catalogue.")
    cs = db.exec(select(ClubSystem).where(
        ClubSystem.club_id == club.id, ClubSystem.system_id == tow.id)).first()
    if cs is None:
        raise RuntimeError("Manchester does not run The Old World (no club_systems row).")
    return club, tow, cs


def seed(force: bool):
    with Session(engine) as db:
        club, tow, cs = _resolve(db)
        existing = db.exec(select(Mission).where(
            Mission.club_id == club.id, Mission.system_id == tow.id)).all()
        if existing:
            if not force:
                print(f"Manchester TOW already has {len(existing)} mission(s). "
                      f"Use --force to delete + reseed.")
                return
            for m in existing:
                if m.image_path:
                    storage.delete_mission_image(m.image_path)
                db.delete(m)
            db.commit()
            print(f"--force: deleted {len(existing)} existing mission(s) + images.")

        created = 0
        for sc in TOW_SCENARIOS:
            image_path = image_url = None
            terrain = sc.get("terrain_path")
            if terrain:
                full = os.path.join(BASE_DIR, terrain)
                if not os.path.exists(full):
                    print(f"  WARN: missing image file {full}, seeding text-only.")
                else:
                    with open(full, "rb") as f:
                        data = f.read()
                    image_path, image_url = storage.upload_mission_image(
                        data, "image/png", club.id, tow.id)
            db.add(Mission(
                club_id=club.id, system_id=tow.id,
                name=sc.get("name"), secondary_objectives=sc.get("secondary_objectives"),
                image_path=image_path, image_url=image_url, active=True))
            created += 1
            print(f"  seeded {sc.get('name')!r} ({'image' if image_url else 'text-only'})")
        db.commit()

        cs.missions_enabled = True
        cs.missions_use_secondary = True
        db.add(cs)
        db.commit()
        print(f"\nSeeded {created} missions for Manchester TOW; "
              f"missions_enabled=True, missions_use_secondary=True.")


def verify():
    with Session(engine) as db:
        club, tow, cs = _resolve(db)
        rows = db.exec(select(Mission).where(
            Mission.club_id == club.id, Mission.system_id == tow.id)).all()
        with_image = sum(1 for m in rows if m.image_url)
        print(f"Manchester TOW: {len(rows)} missions ({with_image} with image), "
              f"missions_enabled={cs.missions_enabled}, "
              f"missions_use_secondary={cs.missions_use_secondary}")
        problems = []
        if len(rows) != len(TOW_SCENARIOS):
            problems.append(f"expected {len(TOW_SCENARIOS)} missions, got {len(rows)}")
        if not cs.missions_enabled:
            problems.append("missions_enabled is False")
        for m in rows:
            if m.image_url:
                r = httpx.get(m.image_url, timeout=15)
                if r.status_code != 200:
                    problems.append(f"image {m.image_url} -> {r.status_code}")
        if problems:
            print("VERIFICATION FAILED:")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print("Verification passed.")


def main():
    if "--verify-only" not in sys.argv:
        seed(force="--force" in sys.argv)
    verify()


if __name__ == "__main__":
    main()
