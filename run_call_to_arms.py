"""Entry point for GitHub Actions: post the weekly TOW Call to Arms.

Mirrors the original's run_scheduled_tow_call_to_arms() / pick_random_tow_scenario()
/ post_tow_call_to_arms_with_image(). Picks a random scenario, posts a flavour
message with the upcoming Wednesday's date, attaching the scenario's terrain
image if one exists. No database access needed.

Note: the original template hardcoded a link to the old streamlit app
(https://calltoarms.streamlit.app/) — that's parametrized to APP_PUBLIC_URL
here instead, since that old URL is no longer the live app.
"""
import os
import json
import random
from datetime import date, timedelta
import httpx

DISCORD_CALL_TO_ARMS_WEBHOOK_URL = os.environ.get("DISCORD_CALL_TO_ARMS_WEBHOOK_URL", "")
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")

COMMON_OBJECTIVES_TOW = "Dead or Fled, The King is Dead, Trophies of War"

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

CALL_TO_ARMS_TEMPLATE = """📣 I SUMMON THE ELECTOR COUNTS 📣

🎲 Scenario of the week: {scenario_name}

- Common Objectives:
{common_objectives}

- Secondary Objectives:
{secondary_objectives}

⚔️ Army Composition Rules: Combined Arms and Grand Melee, Square Based Comp (optional if pre-agreed for competitive matches only) 

Complete the online form if you are coming this Wednesday {wednesday_date}. The recommended start is 18:00-19:00. 

➡️ {app_url}

🤖 Your new AI overlords will pair everybody up based on responses and will make a post on Tuesday evening. If you wish to pre-arrange a game, feel free and just let us know so we can anticipate the numbers.
"""


def pick_random_tow_scenario() -> dict | None:
    if not TOW_SCENARIOS:
        return None
    return random.choice(TOW_SCENARIOS)


def next_wednesday(from_date: date | None = None) -> date:
    if from_date is None:
        from_date = date.today()
    days_ahead = (2 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


def build_tow_call_to_arms_message(scenario: dict, wednesday_date: date) -> str:
    weds_str = wednesday_date.strftime("%d/%m/%Y")
    app_url = APP_PUBLIC_URL or "https://your-app-url"
    return CALL_TO_ARMS_TEMPLATE.format(
        scenario_name=scenario.get("name", "Unknown Scenario"),
        common_objectives=COMMON_OBJECTIVES_TOW,
        secondary_objectives=scenario.get("secondary_objectives", ""),
        wednesday_date=weds_str,
        app_url=app_url,
    )


def post_tow_call_to_arms_with_image(scenario: dict, wednesday_date: date | None = None) -> None:
    if not DISCORD_CALL_TO_ARMS_WEBHOOK_URL:
        print("DISCORD_CALL_TO_ARMS_WEBHOOK_URL not set, skipping.")
        return

    if wednesday_date is None:
        wednesday_date = next_wednesday()

    content = build_tow_call_to_arms_message(scenario, wednesday_date)
    payload = {"content": content}

    terrain_path = scenario.get("terrain_path")
    if terrain_path:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(base_dir, terrain_path)
        if os.path.exists(full_path):
            try:
                with open(full_path, "rb") as f:
                    files = {"file": (os.path.basename(full_path), f, "image/png")}
                    httpx.post(
                        DISCORD_CALL_TO_ARMS_WEBHOOK_URL,
                        data={"payload_json": json.dumps(payload)},
                        files=files,
                        timeout=10,
                    )
                print(f"Posted TOW Call to Arms with terrain image ({terrain_path}).")
                return
            except Exception as e:
                print(f"Failed to post with terrain image, falling back to text: {e}")

    try:
        httpx.post(DISCORD_CALL_TO_ARMS_WEBHOOK_URL, json=payload, timeout=10)
        print("Posted TOW Call to Arms (no terrain image).")
    except Exception as e:
        print(f"Failed to post TOW Call to Arms: {e}")


if __name__ == "__main__":
    scenario = pick_random_tow_scenario()
    if not scenario:
        print("No TOW scenarios configured, skipping.")
    else:
        post_tow_call_to_arms_with_image(scenario)