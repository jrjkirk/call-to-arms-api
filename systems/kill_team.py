"""Hardcoded ruleset for Kill Team.

See systems/old_world.py and systems/__init__.py for the shared rationale.

FACTIONS must stay byte-for-byte identical to KT_FACTIONS in the
frontend's call-to-arms-web/src/lib/signupOptions.ts.
"""

# Canonical system identifier — see systems/old_world.py.
LEGACY_SYSTEM_NAME = "Kill Team"

FACTIONS = [
    "Angels Of Death", "Battleclade", "Blades Of Khaine", "Blooded",
    "Brood Brothers", "Canoptek Circle", "Celestian Insidiants", "Chaos Cult",
    "Corsair Voidscarred", "Death Korps", "Deathwatch", "Elucidian Starstriders",
    "Exaction Squad", "Farstalker Kinband", "Fellgor Ravagers", "Gellerpox Infected",
    "Goremongers", "Hand Of The Archon", "Hearthkyn Salvagers", "Hernkyn Yaegirs",
    "Hierotek Circle", "Hunter Clade", "Imperial Navy Breachers", "Inquisitorial Agents",
    "Kasrkin", "Kommandos", "Legionaries", "Mandrakes", "Murderwing", "Nemesis Claw",
    "Novitiates", "Pathfinders", "Phobos Strike Team", "Plague Marines", "Ratlings",
    "Raveners", "Sanctifiers", "Scout Squad", "Strike Force Variel", "Tempestus Aquilon",
    "Vespid Stingwings", "Void-Dancer Troupe", "Warp Coven", "Wolf Scouts",
    "Wrecka Krew", "Wyrmblade", "XV26 Stealth Battlesuits", "Unlisted Kill Team",
]

ICON_FOLDER = "KT"
