"""Call-to-arms message content — the single source of truth for what each
system's Discord "Call to Arms" post says and how it's assembled.

Split cleanly into two layers so message text can be club-editable without
touching the dynamic bits:

- **Text** (editable): per-system default templates below, tokenized. A club
  can override the template per system (stored in club_settings by callers);
  when unset, the default here is used. `render()` fills the tokens.
- **Functions** (code, not editable): scenario selection + terrain-image
  attachment (The Old World), and the session date / signup URL. These are
  computed in `build_context()` and injected into the template via tokens, so
  editing the surrounding text never breaks mission selection or the image.

Tokens (see `available_tokens`):
  {session_date}          — upcoming session date, DD/MM/YYYY   (all systems)
  {signup_url}            — the app sign-up URL                 (all systems)
  {scenario_name}         — randomly-picked scenario name       (scenario systems)
  {secondary_objectives}  — that scenario's secondary objectives (scenario systems)

Rendering a default template with the same inputs produces byte-identical
output to the pre-refactor standalone scripts (verified).
"""
import json
import os
import random
from datetime import date
from typing import Optional

import httpx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The Old World mission pool. Moved here from run_call_to_arms.py — this is a
# code-defined "function" (random mission selection), not club-editable text.
TOW_SCENARIOS = [
    {"code": "1a", "name": "Upon the Field of Glory", "secondary_objectives": "Baggage Train, Strategic Locations (2), Special Features", "terrain_path": "missions/1a.png"},
    {"code": "1b", "name": "Upon the Field of Glory", "secondary_objectives": "Baggage Train, Strategic Locations (4)", "terrain_path": "missions/1b.png"},
    {"code": "1c", "name": "Upon the Field of Glory", "secondary_objectives": "Baggage Train, Strategic Locations (3), Domination", "terrain_path": "missions/1c.png"},
    {"code": "2a", "name": "King of the Hill", "secondary_objectives": "Baggage Train", "terrain_path": "missions/2a.png"},
    {"code": "2b", "name": "King of the Hill", "secondary_objectives": "Baggage Train, Special Features", "terrain_path": "missions/2b.png"},
    {"code": "2c", "name": "King of the Hill", "secondary_objectives": "Baggage Train", "terrain_path": "missions/2c.png"},
    {"code": "3a", "name": "Drawn Battlelines", "secondary_objectives": "Baggage Train, Strategic Locations (3)", "terrain_path": "missions/3a.png"},
    {"code": "3b", "name": "Drawn Battlelines", "secondary_objectives": "Baggage Train, Strategic Locations (3)", "terrain_path": "missions/3b.png"},
    {"code": "3c", "name": "Drawn Battlelines", "secondary_objectives": "Baggage Train, Strategic Locations (3)", "terrain_path": "missions/3c.png"},
    {"code": "4a", "name": "Close Quarter", "secondary_objectives": "Strategic Locations (2)", "terrain_path": "missions/4a.png"},
    {"code": "4b", "name": "Close Quarter", "secondary_objectives": "Strategic Locations (2)", "terrain_path": "missions/4b.png"},
    {"code": "4c", "name": "Close Quarter", "secondary_objectives": "Strategic Locations (2)", "terrain_path": "missions/4c.png"},
    {"code": "5a", "name": "A Chance Encounter", "secondary_objectives": "Special Features", "terrain_path": "missions/5a.png"},
    {"code": "5b", "name": "A Chance Encounter", "secondary_objectives": "Special Features, Domination", "terrain_path": "missions/5b.png"},
    {"code": "5c", "name": "A Chance Encounter", "secondary_objectives": "Special Features", "terrain_path": "missions/5c.png"},
    {"code": "6a", "name": "Encirclement", "secondary_objectives": "Baggage Train, Special Features, Strategic Locations (4)", "terrain_path": "missions/6a.png"},
    {"code": "6b", "name": "Encirclement", "secondary_objectives": "Baggage Train, Special Features", "terrain_path": "missions/6b.png"},
    {"code": "6c", "name": "Encirclement", "secondary_objectives": "Special Features, Strategic Locations (4)", "terrain_path": "missions/6c.png"},
    {"code": "OB", "name": "Open Battle", "secondary_objectives": "Baggage Train", "terrain_path": None},
]

# system legacy_system_name -> mission pool. A system absent here has no
# scenario tokens and never attaches an image.
SCENARIO_DATA: dict[str, list[dict]] = {
    "The Old World": TOW_SCENARIOS,
}

_BASE_TOKENS = ["session_date", "signup_url"]
_SCENARIO_TOKENS = ["scenario_name", "secondary_objectives"]
_ALL_TOKEN_KEYS = _BASE_TOKENS + _SCENARIO_TOKENS

# Default templates. TOW's common-objectives line is inlined here (it was a
# fixed constant, not dynamic) so it too is editable text.
DEFAULT_TEMPLATES: dict[str, str] = {
    "The Old World": """📣 I SUMMON THE ELECTOR COUNTS 📣

🎲 Scenario of the week: {scenario_name}

- Common Objectives:
Dead or Fled, The King is Dead, Trophies of War

- Secondary Objectives:
{secondary_objectives}

⚔️ Army Composition Rules: Combined Arms and Grand Melee, Square Based Comp (optional if pre-agreed for competitive matches only) 

Complete the online form if you are coming this Wednesday {session_date}. The recommended start is 18:00-19:00. 

➡️ {signup_url}

🤖 Your new AI overlords will pair everybody up based on responses and will make a post on Tuesday evening. If you wish to pre-arrange a game, feel free and just let us know so we can anticipate the numbers.
""",
    "The Horus Heresy": (
        "⚔️ **The Horus Heresy — Call to Arms** ⚔️\n\n"
        "*\"In the long shadow of the Emperor's wrath, brothers turn against brothers. "
        "The galaxy burns, and the loyal and the lost alike must answer the call to war.\"*\n\n"
        "Friday's gathering approaches.  Sign up here: {signup_url}"
    ),
    "Kill Team": (
        "🔪 **Kill Team — Call to Arms** 🔪\n\n"
        "*\"In the cramped corridors and shattered ruins, elite operatives wage their secret wars. "
        "Quick, deadly, decisive — the perfect skirmish awaits.\"*\n\n"
        "Friday's session is approaching. Sign up here: {signup_url}"
    ),
}


def default_template(system: str) -> str:
    return DEFAULT_TEMPLATES.get(system, "")


def available_tokens(system: str) -> list[str]:
    """Tokens a club may use in this system's template."""
    toks = list(_BASE_TOKENS)
    if system in SCENARIO_DATA:
        toks += _SCENARIO_TOKENS
    return toks


def build_context(system: str, session_date: date, signup_url: str) -> tuple[dict, Optional[str]]:
    """Resolve the dynamic token values for one post, plus an optional image
    path to attach. For scenario systems this picks a random mission (the
    preserved "mission selection" function) and exposes its fields."""
    ctx: dict = {
        "session_date": session_date.strftime("%d/%m/%Y"),
        "signup_url": signup_url or "",
    }
    image_path: Optional[str] = None
    scenarios = SCENARIO_DATA.get(system)
    if scenarios:
        sc = random.choice(scenarios)
        ctx["scenario_name"] = sc.get("name", "")
        ctx["secondary_objectives"] = sc.get("secondary_objectives", "")
        terrain_path = sc.get("terrain_path")
        if terrain_path:
            full_path = os.path.join(BASE_DIR, terrain_path)
            if os.path.exists(full_path):
                image_path = full_path
    return ctx, image_path


def render(template: str, context: dict) -> str:
    """Fill known tokens. Uses plain replacement (not str.format) so stray
    braces in edited text can't raise, and unknown {tokens} are left as-is."""
    out = template
    for key in _ALL_TOKEN_KEYS:
        out = out.replace("{" + key + "}", str(context.get(key, "")))
    return out


IMAGE_MODES = ("default", "none", "custom")


def parse_image_setting(value: Optional[str]) -> tuple[str, Optional[str]]:
    """Stored club_settings value -> (mode, url). Unset -> the system default
    (mission image for scenario systems, none otherwise); "none" -> no image;
    anything else is a custom image URL."""
    if not value:
        return "default", None
    if value == "none":
        return "none", None
    return "custom", value


def image_setting_value(mode: str, url: Optional[str]) -> Optional[str]:
    """(mode, url) -> the value to store, or None to clear (= track default)."""
    if mode == "none":
        return "none"
    if mode == "custom" and url and url.strip():
        return url.strip()
    return None


def _post_to_discord(
    webhook_url: str,
    content: str,
    image_path: Optional[str] = None,
    embed_image_url: Optional[str] = None,
) -> None:
    payload: dict = {"content": content}
    if embed_image_url:
        payload["embeds"] = [{"image": {"url": embed_image_url}}]
    if image_path:
        try:
            with open(image_path, "rb") as f:
                files = {"file": (os.path.basename(image_path), f, "image/png")}
                httpx.post(
                    webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=10,
                )
            print(f"Posted call-to-arms with image ({os.path.basename(image_path)}).")
            return
        except Exception as e:
            print(f"Failed to post with image, falling back to text: {e}")
    try:
        httpx.post(webhook_url, json=payload, timeout=10)
        print("Posted call-to-arms (text).")
    except Exception as e:
        print(f"Failed to post call-to-arms: {e}")


def post(
    webhook_url: str,
    template: str,
    system: str,
    session_date: date,
    signup_url: str,
    image_mode: str = "default",
    image_url: Optional[str] = None,
) -> None:
    """Assemble and post one call-to-arms message: build the dynamic context
    (picking a mission for scenario systems), render `template`, and post to
    `webhook_url`. Image handling follows `image_mode`:
      - "default": the system's built-in image (mission terrain for scenario
        systems, none otherwise) — unchanged from before this control existed.
      - "none": text only.
      - "custom": attach `image_url` as a Discord embed image.
    `template` is the caller's resolved text (club override or default)."""
    if not webhook_url:
        print(f"No call-to-arms webhook for {system}, skipping.")
        return
    ctx, default_image_path = build_context(system, session_date, signup_url)
    content = render(template, ctx)

    image_path: Optional[str] = None
    embed_image_url: Optional[str] = None
    if image_mode == "none":
        pass
    elif image_mode == "custom" and image_url:
        embed_image_url = image_url
    else:  # "default"
        image_path = default_image_path

    _post_to_discord(webhook_url, content, image_path=image_path, embed_image_url=embed_image_url)
