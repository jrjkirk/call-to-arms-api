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

from . import age_of_sigmar, horus_heresy, kill_team, middle_earth, old_world, warhammer_40k

_MODULES = (old_world, horus_heresy, kill_team, age_of_sigmar, warhammer_40k, middle_earth)

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


def faction_groups_for(legacy_system_name: str):
    """Grouped faction lists ([{"label", "factions"}, ...]) for systems whose
    module defines FACTION_GROUPS (e.g. Middle Earth's Good/Evil), else None.
    Lets the frontend render <optgroup>s; systems without groups fall back to
    the flat faction_list."""
    module = SYSTEM_RULES.get(legacy_system_name)
    groups = getattr(module, "FACTION_GROUPS", None) if module else None
    if not groups:
        return None
    return [{"label": label, "factions": list(factions)} for label, factions in groups]


def icon_folder_for(legacy_system_name: str):
    """The system's hardcoded icon directory name, or None."""
    module = SYSTEM_RULES.get(legacy_system_name)
    return module.ICON_FOLDER if module else None


# ---------------------------------------------------------------------------
# Authored-or-hardcoded resolution
# ---------------------------------------------------------------------------
# One place that answers "what are this system's rules", so no caller has to
# remember the precedence. DB first, module second — see SystemConfig's comment
# for why that ordering makes the staged migration reversible.
#
# Every one of these takes the SystemConfig row rather than just a name,
# because the authored value lives on it.


def resolved_factions(row) -> list | None:
    """The system's flat faction list: authored if set, else its module's."""
    authored = getattr(row, "faction_list", None)
    if authored:
        return [str(f) for f in authored if str(f).strip()]
    return factions_for(row.legacy_system_name)


def resolved_faction_groups(row) -> list | None:
    """Grouped factions as [{"label", "factions"}], or None for a flat list.

    An authored flat list with no groups deliberately returns None rather than
    falling through to the module's groups: a system whose factions have been
    re-authored without groups is a system with no groups, and inheriting the
    old ones would resurrect labels the admin just removed.
    """
    if getattr(row, "faction_list", None):
        groups = getattr(row, "faction_groups", None)
        if not groups:
            return None
        out = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            label = str(g.get("label", "")).strip()
            factions = [str(f) for f in (g.get("factions") or []) if str(f).strip()]
            if label and factions:
                out.append({"label": label, "factions": factions})
        return out or None
    return faction_groups_for(row.legacy_system_name)


def resolved_icon_folder(row) -> str | None:
    """The directory under icons/ holding this system's faction artwork."""
    authored = (getattr(row, "icon_folder", None) or "").strip()
    if authored:
        return authored
    return icon_folder_for(row.legacy_system_name)


def all_icon_folders():
    """Every registered system's icon directory name, de-duplicated and in
    registration order. Used by the pairings-image renderer to build its
    icon search path without hardcoding folder names — a newly-added system
    module is picked up automatically."""
    seen: dict[str, None] = {}
    for m in _MODULES:
        seen.setdefault(m.ICON_FOLDER, None)
    return list(seen)
