"""Hardcoded ruleset for The Horus Heresy.

See systems/old_world.py and systems/__init__.py for the shared rationale.

FACTIONS must stay byte-for-byte identical to HH_FACTIONS in the
frontend's call-to-arms-web/src/lib/signupOptions.ts.
"""

# Canonical system identifier — see systems/old_world.py.
LEGACY_SYSTEM_NAME = "The Horus Heresy"

FACTIONS = [
    "I - Dark Angels",
    "III - Emperor's Children",
    "IV - Iron Warriors",
    "V - White Scars",
    "VI - Space Wolves",
    "VII - Imperial Fists",
    "VIII - Night Lords",
    "IX - Blood Angels",
    "X - Iron Hands",
    "XII - World Eaters",
    "XIII - Ultramarines",
    "XIV - Death Guard",
    "XV - Thousand Sons",
    "XVI - Sons of Horus",
    "XVII - Word Bearers",
    "XVIII - Salamanders",
    "XIX - Raven Guard",
    "XX - Alpha Legion",
    "Anathema Psykana",
    "Legio Custodes",
    "Mechanicum",
    "Questoris Familia",
    "Solar Auxilia",
]

ICON_FOLDER = "HH"
