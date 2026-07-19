"""Add Age of Sigmar to the SystemConfig catalogue.

One-off script (this repo doesn't manage migrations — see CLAUDE.md /
models.py docstring). Run manually:

    PYTHONPATH=. python seed/seed_age_of_sigmar.py            # create + verify
    PYTHONPATH=. python seed/seed_age_of_sigmar.py --verify-only

Safe to re-run: upsert keyed on `slug`. Mirrors seed/seed_systems_config.py's
shape — see that file for the field-by-field rationale (points-based like
The Old World, no scenarios/battleplans per 2026-07-20 decision).

Faction list and points defaults confirmed with Joel 2026-07-20:
- Factions: systems/age_of_sigmar.py FACTIONS (25 current armies).
- Points: 2000 default / 5000 max (no objection raised to the TOW-style
  defaults proposed).
- No battleplan/scenario dropdown (uses_scenarios=False, explicitly declined).
- Vibe options: Casual/Competitive, matching The Old World.
- Trial club: Manchester — enabled via the admin panel's self-service
  "Add a system" control (POST /admin/club-systems), not this script.
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import SystemConfig
from systems.age_of_sigmar import FACTIONS, ICON_FOLDER, LEGACY_SYSTEM_NAME

ROW = dict(
    name="Age of Sigmar",
    slug="aos",
    legacy_system_name=LEGACY_SYSTEM_NAME,
    uses_points=True,
    default_points=2000,
    max_points=5000,
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
    faction_list=None,  # served from systems/age_of_sigmar.py, not this column
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
    row = session.exec(select(SystemConfig).where(SystemConfig.slug == "aos")).first()
    if row is None:
        problems.append("No SystemConfig row with slug='aos' found.")
        return problems
    if row.legacy_system_name != LEGACY_SYSTEM_NAME:
        problems.append(f"legacy_system_name mismatch: {row.legacy_system_name!r}")
    if not row.active:
        problems.append("Row exists but active=False.")
    from systems import factions_for
    live_factions = factions_for(LEGACY_SYSTEM_NAME)
    if live_factions != FACTIONS:
        problems.append("factions_for() doesn't match systems/age_of_sigmar.py FACTIONS.")
    if len(live_factions) != 25:
        problems.append(f"Expected 25 factions, found {len(live_factions)}.")
    return problems


if __name__ == "__main__":
    verify_only = "--verify-only" in sys.argv
    with Session(engine) as session:
        if not verify_only:
            seed(session)
            print("Seeded/updated Age of Sigmar catalogue row.")
        problems = verify(session)
        if problems:
            print("VERIFICATION FAILED:")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print("Verification passed.")
