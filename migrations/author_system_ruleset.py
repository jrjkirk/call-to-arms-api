"""Move one system's ruleset from its code module into the database.

Phase 4 of the system-authoring work, and deliberately NOT run as part of it.
Six systems are still served by `systems/<name>.py`, and the plan is to migrate
them one at a time, watch each, and only then delete the module.

    PYTHONPATH=. python migrations/author_system_ruleset.py --list
    PYTHONPATH=. python migrations/author_system_ruleset.py --system "Kill Team" --dry-run
    PYTHONPATH=. python migrations/author_system_ruleset.py --system "Kill Team"
    PYTHONPATH=. python migrations/author_system_ruleset.py --system "Kill Team" --revert

## Why one at a time, and why revert exists

`Signup.faction` stores the faction as free text, chosen from whatever list was
in force at signup. If a migration changed a single character of a faction name
the stored value would stop matching the dropdown, and that player's faction
would read as an unlisted value forever after. So this copies the module's list
byte for byte, and refuses if the result would orphan an existing signup.

Reverting is just setting the columns back to NULL. `systems/__init__.py`
resolves DB first and module second, so a NULL sends the system straight back
to the code it is running on today, with nothing lost. The module is the safety
net and stays in the repo until every system is migrated and settled.

## When all six are done

Delete `systems/<name>.py`, drop the module from `_MODULES`, and the
`factions_for` / `faction_groups_for` / `icon_folder_for` fallbacks become
dead code that can go with them. Do not do that while any system still reports
`ruleset_source: "code"` in GET /admin/platform/systems.
"""
import argparse
import sys

from sqlmodel import Session, select

from database import engine
from models import Signup, SystemConfig
from systems import SYSTEM_RULES, faction_groups_for, factions_for, icon_folder_for


def _print_state(db: Session) -> None:
    rows = db.exec(select(SystemConfig).order_by(SystemConfig.name)).all()
    print(f"{'system':<24} {'source':<10} {'factions':>8}  icons")
    for r in rows:
        authored = bool(r.faction_list)
        source = "authored" if authored else ("code" if factions_for(r.legacy_system_name) else "none")
        n = len(r.faction_list or factions_for(r.legacy_system_name) or [])
        icons = r.icon_folder or icon_folder_for(r.legacy_system_name) or "-"
        print(f"{r.name:<24} {source:<10} {n:>8}  {icons}")


def _orphaned_factions(db: Session, system: SystemConfig, factions: list[str]) -> list[str]:
    """Faction values already on signups that the new list would not contain.

    The check that matters. A faction that stops being in the list does not
    break the app, but it does mean a real player's recorded army silently
    stops being a valid option, so it is worth being told about before it
    happens rather than after.
    """
    allowed = set(factions)
    used = db.exec(
        select(Signup.faction)
        .where(Signup.system == system.legacy_system_name)
        .where(Signup.faction.is_not(None))
        .distinct()
    ).all()
    return sorted({f for f in used if f and f not in allowed})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", help="legacy_system_name, e.g. 'Kill Team'")
    ap.add_argument("--list", action="store_true", help="show where each system reads its rules from")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true", help="null the columns; back to the code module")
    args = ap.parse_args()

    with Session(engine) as db:
        if args.list or not args.system:
            _print_state(db)
            if not args.system:
                print("\nPass --system '<legacy name>' to migrate one.")
            return 0

        system = db.exec(
            select(SystemConfig).where(SystemConfig.legacy_system_name == args.system)
        ).first()
        if system is None:
            print(f"No catalogue system with legacy_system_name {args.system!r}.")
            return 1

        if args.revert:
            system.faction_list = None
            system.faction_groups = None
            system.icon_folder = None
            db.add(system)
            db.commit()
            print(f"{system.name}: reverted to its code module.")
            return 0

        module = SYSTEM_RULES.get(system.legacy_system_name)
        if module is None:
            print(f"{system.name} has no code module — nothing to migrate from.")
            return 1

        factions = list(factions_for(system.legacy_system_name) or [])
        groups = faction_groups_for(system.legacy_system_name)
        icons = icon_folder_for(system.legacy_system_name)
        if not factions:
            print(f"{system.name}: its module defines no factions.")
            return 1

        orphans = _orphaned_factions(db, system, factions)
        if orphans:
            print(f"REFUSING: {len(orphans)} faction(s) on existing signups are not in the "
                  f"list being written:\n  " + "\n  ".join(orphans))
            print("\nThat would leave real signups holding a value the dropdown no longer "
                  "offers. Reconcile the module's list first.")
            return 1

        print(f"{system.name}")
        print(f"  factions : {len(factions)}  ({', '.join(factions[:3])}…)")
        print(f"  groups   : {[g['label'] for g in groups] if groups else 'none'}")
        print(f"  icons    : {icons}")
        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0

        system.faction_list = factions
        system.faction_groups = groups
        system.icon_folder = icons
        db.add(system)
        db.commit()
        print(f"\n{system.name} now reads its rules from the database.")
        print("Its module is still in place; --revert puts it back if anything looks wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
