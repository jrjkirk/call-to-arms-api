"""Authoring a game system from the admin UI instead of from a code deploy.

Adding a system used to mean writing `systems/<name>.py`, committing icons to
two repos and deploying both. The catalogue row was already editable; the
ruleset — factions, categories, icon folder — was not.

The block that matters most is the first one. Six systems are still served by
their code modules and are staying that way until each has been migrated and
watched, so an authored system must not disturb them and a module-backed
system must behave exactly as it did before.

Run: PYTHONPATH=. python tests/test_system_authoring.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "authoring.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["SESSION_SECRET"] = "localtestsecret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

import auth  # noqa: E402
import database  # noqa: E402
from main import app  # noqa: E402
from models import Club, SystemConfig, User  # noqa: E402
from systems import (  # noqa: E402
    resolved_faction_groups,
    resolved_factions,
    resolved_icon_folder,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(User(id=1, discord_id="a", discord_name="Joel", club_id=1,
                home_club_id=1, is_platform_admin=True))
    # A system exactly as the six real ones are: catalogue row, no authored
    # ruleset, a matching module in systems/.
    db.add(SystemConfig(id=1, name="The Old World", slug="tow",
                        legacy_system_name="The Old World", active=True,
                        vibe_options=["Casual"], default_vibe="Casual"))
    db.commit()

client = TestClient(app)
client.cookies.set("cta_session", auth._make_session_cookie(1))


print("\n1. A code-backed system is untouched by any of this")
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    check("its faction_list column is still empty", tow.faction_list is None)
    factions = resolved_factions(tow)
    check("but it still resolves factions, from its module",
          factions and "Empire of Man" in factions, str(factions and factions[:2]))
    check("and its icon folder", resolved_icon_folder(tow) == "TOW",
          str(resolved_icon_folder(tow)))
    check("groups are None for a flat system", resolved_faction_groups(tow) is None)

r = client.get("/systems")
tow_out = next(s for s in r.json() if s["slug"] == "tow")
check("the public catalogue serves the module's list",
      "Empire of Man" in (tow_out["faction_list"] or []), str(tow_out["faction_list"])[:60])
check("and reports no uploaded logo", tow_out["logo_url"] is None)


print("\n1b. An EMPTY faction_list is not an authored one")
# The highest-consequence line in this feature. Every one of the six systems on
# prod has faction_list = [] — not NULL — because the original
# seed_systems_config.py wrote an empty array into a column nothing then read.
# `if authored:` treats that as absent and falls through to the module.
# `if authored is not None:` would have switched all six to an empty faction
# list the moment this deployed, and every signup form would have offered no
# factions at all.
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    tow.faction_list = []
    tow.faction_groups = []
    db.add(tow)
    db.commit()
    check("an empty list still resolves to the module's factions",
          len(resolved_factions(db.get(SystemConfig, 1)) or []) == 18,
          str(len(resolved_factions(db.get(SystemConfig, 1)) or [])))
tow_out = next(s for s in client.get("/systems").json() if s["slug"] == "tow")
check("and the public catalogue serves them, not an empty dropdown",
      "Empire of Man" in (tow_out["faction_list"] or []), str(tow_out["faction_list"])[:60])
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    tow.faction_list = None
    tow.faction_groups = None
    db.add(tow)
    db.commit()


print("\n2. A brand-new system is authored entirely through the API")
NEW = {
    "name": "Bolt Action", "slug": "ba", "legacy_system_name": "Bolt Action",
    "uses_points": True, "default_points": 1000, "max_points": 2000,
    "vibe_options": ["Casual", "Competitive"], "default_vibe": "Casual",
    "faction_list": ["Germany", "United States", "Soviet Union", "Great Britain"],
    "icon_folder": "BA",
}
r = client.post("/admin/platform/systems", json=NEW)
check("created", r.status_code == 200, r.text[:160])
new_id = r.json()["id"]

r = client.get("/systems")
ba = next((s for s in r.json() if s["slug"] == "ba"), None)
check("it appears in the public catalogue immediately", ba is not None)
check("with its authored factions",
      ba and ba["faction_list"] == NEW["faction_list"], str(ba and ba["faction_list"]))
check("and its icon folder", ba and ba["icon_folder"] == "BA")
check("no module exists for it, and nothing breaks",
      ba is not None and ba["faction_groups"] is None)


print("\n3. Categories, the Middle Earth shape, but authored")
GROUPED = {
    **NEW, "name": "Saga", "slug": "saga", "legacy_system_name": "Saga",
    "faction_list": ["Vikings", "Normans", "Byzantines", "Moors"],
    "faction_groups": [
        {"label": "Age of Vikings", "factions": ["Vikings", "Normans"]},
        {"label": "Age of Crusades", "factions": ["Byzantines", "Moors"]},
    ],
    "icon_folder": "SAGA",
}
r = client.post("/admin/platform/systems", json=GROUPED)
check("created with categories", r.status_code == 200, r.text[:160])
saga = next(s for s in client.get("/systems").json() if s["slug"] == "saga")
check("groups come back in order",
      [g["label"] for g in saga["faction_groups"]] == ["Age of Vikings", "Age of Crusades"],
      str(saga["faction_groups"]))
check("and the flat list is still served alongside",
      saga["faction_list"] == GROUPED["faction_list"])

# A category naming a faction that is not in the list would show a dropdown
# option that then fails validation on submit.
r = client.post("/admin/platform/systems", json={
    **GROUPED, "slug": "saga2", "legacy_system_name": "Saga Two",
    "faction_groups": [{"label": "Age of Vikings", "factions": ["Vikings", "Klingons"]}],
})
check("a category cannot cover only part of the list", r.status_code == 422, r.text[:120])

r = client.post("/admin/platform/systems", json={
    **GROUPED, "slug": "saga3", "legacy_system_name": "Saga Three",
    "faction_groups": [{"label": "Only some", "factions": ["Vikings"]}],
})
check("and cannot leave factions uncategorised", r.status_code == 422, r.text[:120])


print("\n4. Editing the ruleset, and what empty means")
r = client.post(f"/admin/platform/systems/{new_id}", json={
    "name": "Bolt Action", "legacy_system_name": "Bolt Action",
    "uses_points": True, "default_points": 1000, "max_points": 2000,
    "vibe_options": ["Casual"], "default_vibe": "Casual",
    "faction_list": ["Germany", "Germany", " United States ", ""],
    "icon_folder": "BA",
})
check("edit accepted", r.status_code == 200, r.text[:160])
ba = next(s for s in client.get("/systems").json() if s["slug"] == "ba")
check("blanks dropped and duplicates collapsed",
      ba["faction_list"] == ["Germany", "United States"], str(ba["faction_list"]))

# Emptying the list is how a system is handed back to a code module. For a
# system with no module that means "no factions yet", not "[]" — the two must
# be distinguishable or the fallback can never fire.
r = client.post(f"/admin/platform/systems/{new_id}", json={
    "name": "Bolt Action", "legacy_system_name": "Bolt Action",
    "vibe_options": ["Casual"], "default_vibe": "Casual",
    "faction_list": [], "icon_folder": "BA",
})
with Session(database.engine) as db:
    check("an empty list stores NULL, not []",
          db.get(SystemConfig, new_id).faction_list is None)


print("\n5. Authoring a system that HAS a module overrides it, reversibly")
# This is the staged migration in miniature: populate the column, the authored
# value wins; null it, and the module takes over again with nothing lost.
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    tow.faction_list = ["Empire of Man", "Skaven"]
    db.add(tow)
    db.commit()
tow_out = next(s for s in client.get("/systems").json() if s["slug"] == "tow")
check("the authored list wins over the module",
      tow_out["faction_list"] == ["Empire of Man", "Skaven"], str(tow_out["faction_list"]))

with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    tow.faction_list = None
    db.add(tow)
    db.commit()
tow_out = next(s for s in client.get("/systems").json() if s["slug"] == "tow")
check("nulling it falls straight back to the module",
      len(tow_out["faction_list"]) == 18, str(len(tow_out["faction_list"] or [])))


print("\n6. The icon checklist tells you exactly what to add and where")
r = client.get(f"/admin/platform/systems/{new_id}/icon-checklist")
check("checklist returned", r.status_code == 200, r.text[:120])
cl = r.json()
check("both destinations named",
      cl["destinations"]["api_png"].endswith("icons/BA/")
      and cl["destinations"]["web"].endswith("static/icons/BA/"),
      str(cl["destinations"]))
# The slug rule has three implementations in two languages; a drift here means
# the renderer looks for a file the checklist never told anyone to create.
r2 = client.post(f"/admin/platform/systems/{new_id}", json={
    "name": "Bolt Action", "legacy_system_name": "Bolt Action",
    "vibe_options": ["Casual"], "default_vibe": "Casual",
    "faction_list": ["Orc & Goblin Tribes"], "icon_folder": "BA",
})
cl = client.get(f"/admin/platform/systems/{new_id}/icon-checklist").json()
check("filenames use the same slug rule as the renderer",
      cl["factions"][0]["png"] == "orc_and_goblin_tribes.png",
      str(cl["factions"][0]))
check("and it says which are still missing", cl["missing_png_count"] == 1, str(cl))

from scripts.render_pairings_image import _faction_slug as renderer_slug  # noqa: E402
check("proved against the renderer's own function, not a copy of it",
      renderer_slug("Orc & Goblin Tribes") == cl["factions"][0]["slug"])


print("\n7. Only a platform admin can author")
with Session(database.engine) as db:
    db.add(User(id=2, discord_id="b", discord_name="Club admin", club_id=1,
                home_club_id=1, is_super_admin=True))
    db.commit()
club_admin = TestClient(app)
club_admin.cookies.set("cta_session", auth._make_session_cookie(2))
check("a club super-admin cannot create a system",
      club_admin.post("/admin/platform/systems", json={
          **NEW, "slug": "nope", "legacy_system_name": "Nope"}).status_code == 403)
check("nor read the checklist",
      club_admin.get(f"/admin/platform/systems/{new_id}/icon-checklist").status_code == 403)


print("\n8. Slug and legacy name stay unique")
check("duplicate slug refused",
      client.post("/admin/platform/systems", json={
          **NEW, "legacy_system_name": "Something Else"}).status_code == 409)
check("duplicate legacy name refused",
      client.post("/admin/platform/systems", json={
          **NEW, "slug": "different"}).status_code == 409)

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
