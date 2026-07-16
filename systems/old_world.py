"""Hardcoded ruleset for The Old World.

A system's *rules* — its faction list and icon directory — live here in
versioned code, never as editable database rows and never exposed through
an admin form. This is deliberately separate from the SystemConfig
catalogue (which systems a club has enabled, its schedule, and the
points/vibe/scenario form config): that part stays DB-driven and
self-service. See systems/__init__.py for the registry and rationale.

FACTIONS must stay byte-for-byte identical to TOW_FACTIONS in the
frontend's call-to-arms-web/src/lib/signupOptions.ts — a silent drift
would change the faction options real users see.
"""

# Canonical system identifier. Signup.system, Pairing.system,
# PublishState.system and SystemConfig.legacy_system_name all hold this
# exact string; it — not the `tow` slug — is what runtime data keys on.
LEGACY_SYSTEM_NAME = "The Old World"

FACTIONS = [
    "Empire of Man", "Dwarfen Mountain Holds", "Kingdom of Bretonnia",
    "Wood Elf Realms", "High Elf Realms", "Orc & Goblin Tribes",
    "Warriors of Chaos", "Beastmen Brayheards", "Tomb Kings of Khemri",
    "Skaven", "Ogre Kingdoms", "Lizardmen", "Chaos Dwarfs", "Dark Elves",
    "Daemons of Chaos", "Vampire Counts", "Grand Cathay", "Renegade Crowns",
]

ICON_FOLDER = "TOW"
