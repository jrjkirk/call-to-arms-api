"""Registry of hardcoded per-system rulesets.

Each system's *rules* — its faction list and icon directory — are defined
in a versioned Python module here (one per real system), not stored as
editable database rows or exposed through any admin form. This is the one
place the rest of the backend looks up a system's hardcoded ruleset.

Deliberately separate from the SystemConfig catalogue: the catalogue owns
which systems exist, which a club has enabled, its schedule, and the
points/vibe/scenario form config — all DB-driven and self-service. The
faction/icon ruleset is not editable data and lives in code instead.

Modules are keyed by `legacy_system_name` (the full display string, e.g.
"The Old World"). That is the canonical identifier stored in
Signup.system / Pairing.system and used throughout pairings_engine.py and
signups.py — not the catalogue's short `slug`. A catalogue system with no
module here (e.g. a newly-added one) simply has no hardcoded ruleset yet;
the accessors return None so callers can fall back cleanly.
"""

from . import horus_heresy, kill_team, old_world

_MODULES = (old_world, horus_heresy, kill_team)

# legacy_system_name -> rules module
SYSTEM_RULES = {m.LEGACY_SYSTEM_NAME: m for m in _MODULES}


def rules_for(legacy_system_name: str):
    """Return the hardcoded rules module for a system, or None if the
    system has no hardcoded ruleset (e.g. a new catalogue-only system)."""
    return SYSTEM_RULES.get(legacy_system_name)


def factions_for(legacy_system_name: str):
    """The system's hardcoded faction list (a fresh copy), or None."""
    module = SYSTEM_RULES.get(legacy_system_name)
    return list(module.FACTIONS) if module else None


def icon_folder_for(legacy_system_name: str):
    """The system's hardcoded icon directory name, or None."""
    module = SYSTEM_RULES.get(legacy_system_name)
    return module.ICON_FOLDER if module else None


def all_icon_folders():
    """Every registered system's icon directory name, de-duplicated and in
    registration order. Used by the pairings-image renderer to build its
    icon search path without hardcoding folder names — a newly-added system
    module is picked up automatically."""
    seen: dict[str, None] = {}
    for m in _MODULES:
        seen.setdefault(m.ICON_FOLDER, None)
    return list(seen)
