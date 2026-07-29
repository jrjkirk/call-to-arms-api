"""Add Middle Earth (Middle-earth SBG) to the SystemConfig catalogue.

One-off script (this repo doesn't manage migrations — see CLAUDE.md /
models.py docstring). Run manually:

    PYTHONPATH=. python seed/seed_middle_earth.py            # create + verify
    PYTHONPATH=. python seed/seed_middle_earth.py --verify-only

Safe to re-run: upsert keyed on `slug`. Mirrors seed/seed_age_of_sigmar.py.

Config confirmed with Joel 2026-07-29:
- Display name "Middle Earth", slug "mesbg".
- Factions: systems/middle_earth.py, grouped Good/Evil (FACTION_GROUPS) — the
  frontend renders those as optgroups; the flat FACTIONS list is derived.
- Points: 700 default / 1500 max. Vibes: Casual/Competitive.
- No scenario dropdown (uses_scenarios=False). has_league=False (self-enable
  per club later).
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import SystemConfig
from systems.middle_earth import FACTIONS, FACTION_GROUPS, ICON_FOLDER, LEGACY_SYSTEM_NAME

ROW = dict(
    name="Middle Earth",
    slug="mesbg",
    legacy_system_name=LEGACY_SYSTEM_NAME,
    uses_points=True,
    default_points=700,
    max_points=1500,
    vibe_options=["Casual", "Competitive"],
    default_vibe="Casual",
    uses_scenarios=False,
    scenario_options=None,
    default_scenario=None,
    allows_demo=True,
    has_intro_prepass=True,
    has_league=False,
    recent_weeks=3,
    extended_weeks=6,
    faction_list=None,  # served from systems/middle_earth.py, not this column
    icon_folder=ICON_FOLDER,
    active=True,
)


def seed(session: Session):
    existing = session.exec(select(SystemConfig).where(SystemConfig.slug == ROW["slug"])).first()
    if existing:
        for k, v in ROW.items():
            setattr(existing, k, v)
        session.add(existing)
    else:
        session.add(SystemConfig(**ROW))
    session.commit()


def verify(session: Session) -> list[str]:
    problems: list[str] = []
    row = session.exec(select(SystemConfig).where(SystemConfig.slug == "mesbg")).first()
    if row is None:
        problems.append("No SystemConfig row with slug='mesbg' found.")
        return problems
    if row.legacy_system_name != LEGACY_SYSTEM_NAME:
        problems.append(f"legacy_system_name mismatch: {row.legacy_system_name!r}")
    if not row.active:
        problems.append("Row exists but active=False.")
    from systems import factions_for, faction_groups_for
    live_factions = factions_for(LEGACY_SYSTEM_NAME)
    if live_factions != FACTIONS:
        problems.append("factions_for() doesn't match systems/middle_earth.py FACTIONS.")
    groups = faction_groups_for(LEGACY_SYSTEM_NAME)
    if not groups or [g["label"] for g in groups] != ["Good", "Evil"]:
        problems.append(f"faction_groups_for() not [Good, Evil]: {groups and [g['label'] for g in groups]}")
    elif sum(len(g["factions"]) for g in groups) != len(FACTIONS):
        problems.append("group factions don't sum to the flat FACTIONS count.")
    return problems


if __name__ == "__main__":
    verify_only = "--verify-only" in sys.argv
    with Session(engine) as session:
        if not verify_only:
            seed(session)
            print(f"Seeded/updated Middle Earth catalogue row ({len(FACTIONS)} factions).")
        problems = verify(session)
        if problems:
            print("VERIFICATION FAILED:")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print("Verification passed.")
