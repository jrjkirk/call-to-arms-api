"""Hardcoded ruleset for Middle Earth (Middle-earth SBG).

See systems/old_world.py and systems/__init__.py for the shared rationale.

Unlike the other systems, MESBG has ~90 army lists, so they're grouped by
alignment (Good / Evil) — FACTION_GROUPS drives the grouped dropdowns; FACTIONS
is the flat list (derived) used everywhere a flat list is expected (validation,
faction_list, icon lookup).
"""

# Canonical system identifier — see systems/old_world.py.
LEGACY_SYSTEM_NAME = "Middle Earth"

# (group label, factions) — order preserved; drives the Good/Evil optgroups.
FACTION_GROUPS = [
    ("Good", [
        "Arathorn's Stand", "Army of Dale", "Army of Edoras", "Army of Erebor",
        "Army of Lake-town", "Army of Thror", "Arnor", "Assault on Ravenhill",
        "Atop the Wall", "Battle of Bywater", "The Battle of Five Armies",
        "Battle of Fornost", "The Beornings", "Breaking of the Fellowship",
        "Defenders of Erebor", "Defenders of Helm's Deep",
        "Defenders of the Hornburg", "Defenders of the Pelennor", "The Eagles",
        "Erebor & Dale", "Erebor Reclaimed", "Fangorn", "The Fellowship",
        "The Fiefdoms", "Fords of Isen", "Garrison of Dale",
        "Garrison of Ithilien", "The Grey Company", "The Grief of Eomer",
        "Halls of Thranduil", "The Iron Hills", "Kingdom of Khazad-dum",
        "Kingdom of Rohan", "The Last Alliance", "Lindon", "Lothlorien",
        "Men of the West", "Minas Tirith", "Numenor", "Paths of the Druadan",
        "Radagast's Alliance", "Rangers of Mirkwood", "Realms of Men",
        "Reclamation of Osgiliath", "Return of the King", "Ride Out",
        "Riders of Eomer", "Riders of Theoden", "Rivendell",
        "Road to Helm's Deep", "Road to Rivendell", "The Shire",
        "Survivors of Lake-town", "Thorin's Company", "The White Council",
    ]),
    ("Evil", [
        "Army of Carn Dum", "Army of Gothmog", "Army of Gundabad",
        "Army of the Great Eye", "Army of the White Hand",
        "Assault Upon Helm's Deep", "Azog's Hunters", "Barad-Dur",
        "Besiegers of the Hornburg", "The Black Gate", "The Black Riders",
        "Buhrdur's Horde", "Cirith Ungol", "Corsair Fleets", "Depths of Moria",
        "Desolator of the North", "Dragons of the North", "The Easterlings",
        "Goblin-Town", "Grand Army of the South", "Harad",
        "Host of the Dragon Emperor", "Host of the Witch-King",
        "Legions of Mordor", "Lurtz's Scouts", "Minas Morgul", "Moria",
        "Muster of Isengard", "Pits of Dol Guldur", "Rise of the Necromancer",
        "The Serpent Horde", "Shadows of Angmar", "Sharkey's Rogues",
        "The Three Trolls", "Ugluk's Scouts", "Umbar", "Usurpers of Edoras",
        "Wolf Pack of Angmar", "Wolves of Isengard", "Wraiths on Wings",
    ]),
]

# Flat list, derived from the groups — the shape the rest of the stack expects.
FACTIONS = [f for _label, factions in FACTION_GROUPS for f in factions]

ICON_FOLDER = "MESBG"
