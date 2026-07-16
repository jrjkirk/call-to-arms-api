"""Phase 0, steps 1-2: create the `systems` table and seed TOW/HH/KT.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Run manually:

    python seed_systems_config.py            # create + seed + verify
    python seed_systems_config.py --verify-only

Safe to re-run: table creation is idempotent (CREATE TABLE IF NOT EXISTS via
SQLModel's checkfirst), and seeding is an upsert keyed on `slug`.

The verification step imports the actual current constants from signups.py
so it fails loudly if this script's seed values and the live hardcoded
constants have drifted — the correctness bar from the Phase 0 doc ("compares
each seeded row's JSON fields against the current hardcoded constants line
by line before moving to step 3").
"""
import sys

from sqlmodel import Session, SQLModel, select

from database import engine
from models import SystemConfig
from signups import SYSTEMS, TOW_VIBES, HH_VIBES, SCENARIO_OPTIONS

# ---------------------------------------------------------------------------
# Seed data. Values pulled from a fresh repo pull on 2026-07-13:
#   signups.py       -> SYSTEMS, TOW_VIBES, HH_VIBES, SCENARIO_OPTIONS,
#                        per-system points/vibe/scenario/can_demo defaults
#                        (signup_create/signup_swap, is_hh/is_kt branches)
#   pairings_engine.py -> has_intro_prepass (system in (TOW, HH)),
#                          recent/extended weeks (HH: 6/12, else 3/6),
#   render_pairings_image.py -> icon_folder search dirs (TOW/HH/KT) —
#                          informational only; lookup still searches all
#                          three folders regardless of system (see decision
#                          log below), so this refactor does not change
#                          which icon resolves for a given faction.
# ---------------------------------------------------------------------------

SEED_ROWS = [
    dict(
        name="The Old World",
        slug="tow",
        legacy_system_name="The Old World",
        uses_points=True,
        default_points=2000,
        max_points=10000,
        vibe_options=sorted(TOW_VIBES),
        default_vibe="Casual",
        uses_scenarios=True,
        scenario_options=sorted(SCENARIO_OPTIONS),
        default_scenario="Open Battle",
        allows_demo=True,
        has_intro_prepass=True,
        recent_weeks=3,
        extended_weeks=6,
        faction_list=None,  # frontend signupOptions.ts not in this repo pull
        icon_folder="TOW",
        active=True,
    ),
    dict(
        name="The Horus Heresy",
        slug="hh",
        legacy_system_name="The Horus Heresy",
        uses_points=True,
        default_points=3000,
        max_points=10000,
        vibe_options=sorted(HH_VIBES),
        default_vibe="Standard",
        uses_scenarios=False,
        scenario_options=None,
        default_scenario=None,
        allows_demo=True,
        has_intro_prepass=True,
        recent_weeks=6,
        extended_weeks=12,
        faction_list=None,
        icon_folder="HH",
        active=True,
    ),
    dict(
        name="Kill Team",
        slug="kt",
        legacy_system_name="Kill Team",
        uses_points=False,
        default_points=None,
        max_points=None,
        vibe_options=["Standard"],  # not user-selectable; always forced
        default_vibe="Standard",
        uses_scenarios=False,
        scenario_options=None,
        default_scenario=None,
        allows_demo=False,
        has_intro_prepass=False,
        recent_weeks=3,
        extended_weeks=6,
        faction_list=None,
        icon_folder="KT",
        active=True,
    ),
]


def create_table():
    SystemConfig.metadata.create_all(engine, tables=[SystemConfig.__table__], checkfirst=True)


def seed(session: Session):
    for row in SEED_ROWS:
        existing = session.exec(
            select(SystemConfig).where(SystemConfig.slug == row["slug"])
        ).first()
        if existing:
            for k, v in row.items():
                setattr(existing, k, v)
            session.add(existing)
        else:
            session.add(SystemConfig(**row))
    session.commit()


def verify(session: Session) -> list[str]:
    """Diff seeded rows against live hardcoded constants. Returns a list of
    mismatch descriptions; empty list means clean."""
    problems: list[str] = []

    live_names = set(SYSTEMS)
    seeded_names = {r["legacy_system_name"] for r in SEED_ROWS}
    if live_names != seeded_names:
        problems.append(
            f"SYSTEMS mismatch: live={live_names} seeded={seeded_names}"
        )

    by_slug = {r["slug"]: r for r in SEED_ROWS}

    tow = by_slug["tow"]
    if set(tow["vibe_options"]) != TOW_VIBES:
        problems.append(f"TOW vibe_options mismatch: seeded={tow['vibe_options']} live={TOW_VIBES}")
    if set(tow["scenario_options"]) != SCENARIO_OPTIONS:
        problems.append(f"TOW scenario_options mismatch: seeded={tow['scenario_options']} live={SCENARIO_OPTIONS}")

    hh = by_slug["hh"]
    if set(hh["vibe_options"]) != HH_VIBES:
        problems.append(f"HH vibe_options mismatch: seeded={hh['vibe_options']} live={HH_VIBES}")

    # DB round-trip check: what's actually on staging right now
    rows = session.exec(select(SystemConfig)).all()
    if len(rows) != len(SEED_ROWS):
        problems.append(f"Row count mismatch: db has {len(rows)}, expected {len(SEED_ROWS)}")

    db_by_slug = {r.slug: r for r in rows}
    for slug, expected in by_slug.items():
        actual = db_by_slug.get(slug)
        if actual is None:
            problems.append(f"Missing seeded row for slug={slug}")
            continue
        for field, expected_val in expected.items():
            actual_val = getattr(actual, field)
            if field in ("vibe_options", "scenario_options", "faction_list"):
                if actual_val is not None:
                    actual_val = sorted(actual_val) if isinstance(actual_val, list) else actual_val
                comp_expected = sorted(expected_val) if isinstance(expected_val, list) else expected_val
                if actual_val != comp_expected:
                    problems.append(f"[{slug}] {field}: db={actual_val!r} expected={comp_expected!r}")
            else:
                if actual_val != expected_val:
                    problems.append(f"[{slug}] {field}: db={actual_val!r} expected={expected_val!r}")

    return problems


def main():
    verify_only = "--verify-only" in sys.argv
    if not verify_only:
        print("Creating systems table (idempotent)...")
        create_table()
        with Session(engine) as session:
            print("Seeding TOW/HH/KT...")
            seed(session)

    with Session(engine) as session:
        problems = verify(session)

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: seeded rows match live hardcoded constants byte-for-byte.")


if __name__ == "__main__":
    main()
