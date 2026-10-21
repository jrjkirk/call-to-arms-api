"""A game system's ruleset: its faction list, faction groups and icon folder.

All of it is authored data on the SystemConfig row, edited by a platform admin
at POST /admin/platform/systems/{id}. Callers ask through the `resolved_*`
functions below rather than reading the columns, so the cleaning (blank names
dropped, malformed groups skipped) happens in one place.

Until 2026-10-21 the six original systems (The Old World, Horus Heresy, Kill
Team, Age of Sigmar, 40k, Middle Earth) were served by hardcoded modules here,
one `systems/<name>.py` each, with the DB column as an override. Their lists
were copied into the DB byte for byte by migrations/author_system_ruleset.py,
and the modules were deleted. Git history has them if one is ever needed.

The package itself stays because every caller imports from `systems`.
"""

import os


def resolved_factions(row) -> list | None:
    """The system's flat faction list, or None if it has none yet."""
    authored = getattr(row, "faction_list", None)
    if authored:
        return [str(f) for f in authored if str(f).strip()]
    return None


def resolved_faction_groups(row) -> list | None:
    """Grouped factions as [{"label", "factions"}], or None for a flat list."""
    if not getattr(row, "faction_list", None):
        return None
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


def resolved_icon_folder(row) -> str | None:
    """The directory under icons/ holding this system's faction artwork."""
    authored = (getattr(row, "icon_folder", None) or "").strip()
    return authored or None


def all_icon_folders() -> list[str]:
    """Every subdirectory of the repo's icons/, sorted. The pairings-image
    renderer searches all of them for a faction's icon.

    Read from disk rather than from the DB because the renderer needs the
    files, not the rows, and this way an authored system's folder is found the
    moment its icons are committed. (When the list came from the code modules,
    Bolt Action's and StarCraft's folders were never searched.)
    """
    icons_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")
    try:
        entries = os.listdir(icons_root)
    except FileNotFoundError:
        return []
    return sorted(e for e in entries if os.path.isdir(os.path.join(icons_root, e)))
