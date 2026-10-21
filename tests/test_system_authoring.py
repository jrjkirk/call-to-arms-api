"""Authoring a game system from the admin UI instead of from a code deploy.

Adding a system used to mean writing `systems/<name>.py`, committing icons to
two repos and deploying both. The catalogue row was already editable; the
ruleset — factions, categories, icon folder — was not.

The six original systems were served by code modules until 2026-10-21, when
their lists were copied into the database and the modules deleted. The first
block pins what they are now: an ordinary authored row, and the only source.

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

TOW_FACTIONS = ["Empire of Man", "Dwarfen Mountain Holds", "Kingdom of Bretonnia"]


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc", active=True))
    db.add(User(id=1, discord_id="a", discord_name="Joel", club_id=1,
                home_club_id=1, is_platform_admin=True))
    # A system exactly as the six original ones are since their migration: the
    # ruleset lives on the row, and there is no module behind it.
    db.add(SystemConfig(id=1, name="The Old World", slug="tow",
                        legacy_system_name="The Old World", active=True,
                        vibe_options=["Casual"], default_vibe="Casual",
                        faction_list=TOW_FACTIONS, icon_folder="TOW"))
    db.commit()

client = TestClient(app)
client.cookies.set("cta_session", auth._make_session_cookie(1))


print("\n1. A migrated system is served from its own row")
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    check("it resolves its factions", resolved_factions(tow) == TOW_FACTIONS,
          str(resolved_factions(tow)))
    check("and its icon folder", resolved_icon_folder(tow) == "TOW",
          str(resolved_icon_folder(tow)))
    check("groups are None for a flat system", resolved_faction_groups(tow) is None)

r = client.get("/systems")
tow_out = next(s for s in r.json() if s["slug"] == "tow")
check("the public catalogue serves that list",
      tow_out["faction_list"] == TOW_FACTIONS, str(tow_out["faction_list"])[:60])
check("and reports no uploaded logo", tow_out["logo_url"] is None)


print("\n1b. With no list, a system has no factions, and nothing else steps in")
# Before the modules were deleted, an empty list fell through to them. Now
# there is nothing behind the column, and an empty one must read as "none"
# rather than crash or resurrect something.
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    tow.faction_list = []
    tow.faction_groups = []
    tow.icon_folder = None
    db.add(tow)
    db.commit()
    tow = db.get(SystemConfig, 1)
    check("an empty list resolves to None", resolved_factions(tow) is None)
    check("and so do its groups and icons",
          resolved_faction_groups(tow) is None and resolved_icon_folder(tow) is None)
tow_out = next(s for s in client.get("/systems").json() if s["slug"] == "tow")
check("the public catalogue serves no list", not tow_out["faction_list"],
      str(tow_out["faction_list"]))
with Session(database.engine) as db:
    tow = db.get(SystemConfig, 1)
    tow.faction_list = TOW_FACTIONS
    tow.icon_folder = "TOW"
    db.add(tow)
    db.commit()


print("\n2. A brand-new system is authored entirely through the API")
NEW = {
    "name": "Bolt Action", "slug": "ba", "legacy_system_name": "Bolt Action",
    "uses_points": True, "default_points": 1000, "max_points": 2000,
    "vibe_options": ["Casual", "Competitive"], "default_vibe": "Casual",
    "faction_list": ["Germany", "United States", "Soviet Union", "Great Britain"],
    "icon_folder": "BA",
    # Custom, so the checks below can compare against the order as written.
    # Section 7 covers the alphabetical default.
    "faction_order": "custom",
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
check("with no groups",
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

# An emptied list is stored as NULL, "no factions yet", rather than [].
r = client.post(f"/admin/platform/systems/{new_id}", json={
    "name": "Bolt Action", "legacy_system_name": "Bolt Action",
    "vibe_options": ["Casual"], "default_vibe": "Casual",
    "faction_list": [], "icon_folder": "BA",
})
with Session(database.engine) as db:
    check("an empty list stores NULL, not []",
          db.get(SystemConfig, new_id).faction_list is None)


print("\n5. Adding a faction to a migrated system takes effect at once")
# The request that finished the migration: Kill Team wanted new teams without
# a code deploy. Editing an original system is now just an authored edit.
r = client.post("/admin/platform/systems/1", json={
    "name": "The Old World", "legacy_system_name": "The Old World",
    "vibe_options": ["Casual"], "default_vibe": "Casual",
    "faction_list": TOW_FACTIONS + ["Tomb Kings of Khemri"], "icon_folder": "TOW",
})
check("edit accepted", r.status_code == 200, r.text[:160])
tow_out = next(s for s in client.get("/systems").json() if s["slug"] == "tow")
check("the new faction is served, the rest untouched and in order",
      tow_out["faction_list"] == TOW_FACTIONS + ["Tomb Kings of Khemri"],
      str(tow_out["faction_list"]))
rows = {r["id"]: r for r in client.get("/admin/platform/systems").json()}
check("and the platform panel reports it as authored",
      rows[1]["ruleset_source"] == "authored", rows[1]["ruleset_source"])


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


print("\n7. Faction order")
r = client.post("/admin/platform/systems", json={
    **NEW, "name": "Infinity", "slug": "inf", "legacy_system_name": "Infinity",
    "faction_list": ["Yu Jing", "Unlisted Army", "Ariadna", "PanOceania"],
    "icon_folder": "INF", "faction_order": "alphabetical",
})
check("created alphabetical", r.status_code == 200, r.text[:160])
inf_id = r.json()["id"]
check("sorted, catch-all last",
      r.json()["faction_list"] == ["Ariadna", "PanOceania", "Yu Jing", "Unlisted Army"],
      str(r.json()["faction_list"]))
r = client.post("/admin/platform/systems", json={
    k: v for k, v in {**NEW, "name": "Saga X", "slug": "sagax", "legacy_system_name": "Saga X",
                      "faction_list": ["b", "a"]}.items() if k != "faction_order"})
check("a new system with no order given defaults to alphabetical",
      r.status_code == 200 and r.json()["faction_order"] == "alphabetical"
      and r.json()["faction_list"] == ["a", "b"], r.text[:160])
EDIT = {"name": "Infinity", "legacy_system_name": "Infinity",
        "vibe_options": ["Casual"], "default_vibe": "Casual", "icon_folder": "INF"}
r = client.post(f"/admin/platform/systems/{inf_id}", json={**EDIT,
      "faction_list": ["Yu Jing", "Ariadna", "Nomads"]})
check("an edit that doesn't mention the order keeps it",
      r.json()["faction_order"] == "alphabetical"
      and r.json()["faction_list"] == ["Ariadna", "Nomads", "Yu Jing"], str(r.json()["faction_list"]))
r = client.post(f"/admin/platform/systems/{inf_id}", json={**EDIT,
      "faction_list": ["Yu Jing", "Ariadna", "Nomads"], "faction_order": "custom"})
check("custom keeps the order as written",
      r.json()["faction_list"] == ["Yu Jing", "Ariadna", "Nomads"], str(r.json()["faction_list"]))
r = client.post(f"/admin/platform/systems/{inf_id}", json={**EDIT,
      "faction_list": ["Yu Jing"], "faction_order": "random"})
check("an unknown order is refused", r.status_code == 422, r.text[:120])

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
