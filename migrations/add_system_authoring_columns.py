"""Give `systems` the columns needed to author a system from the admin UI.

Adding a game system has always needed a code change: `systems/<name>.py`
carries the faction list, the faction groups and the icon folder, and only a
deploy could introduce one. The catalogue row itself was already editable —
this closes the other half.

    faction_groups  JSONB  [{"label": ..., "factions": [...]}, ...]
    logo_path       text   Supabase Storage object path
    logo_url        text   public URL for the uploaded logo

`faction_list` and `icon_folder` already exist and are, in main.py's own words,
"(dead) SystemConfig.faction_list / icon_folder DB columns" — nothing has ever
read them. This migration does not add them; it is the code change alongside it
that brings them to life.

## The resolution order is DB first, code second

    faction_list = row.faction_list or module.FACTIONS

Deliberately that way round, because the six existing systems are staying in
code until they have been tested one at a time. DB-first makes that migration
reversible per system: populate one system's column, watch it, and if anything
looks wrong set the column back to NULL and it falls straight through to the
module again. Code-first would have meant DB edits on those six were silently
ignored, which is a confusing afternoon waiting to happen.

Every column is nullable and stays nullable. NULL means "this system has no
authored value, ask the code module", which is exactly the state all six
existing rows are in and must remain in until they are migrated.

One-off script, not a long-lived migration tool (see CLAUDE.md / models.py
docstring). Additive + idempotent, so it is safe to re-run.

    PYTHONPATH=. python migrations/add_system_authoring_columns.py
    PYTHONPATH=. python migrations/add_system_authoring_columns.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine

_COLUMNS = {
    "faction_groups": "JSONB",
    "logo_path": "TEXT",
    "logo_url": "TEXT",
}


def add_columns():
    with Session(engine) as session:
        for name, sql_type in _COLUMNS.items():
            session.exec(text(
                f"ALTER TABLE systems ADD COLUMN IF NOT EXISTS {name} {sql_type}"
            ))
        session.commit()
    print("Added systems." + ", systems.".join(_COLUMNS) + " (or they were already there).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        for name, sql_type in _COLUMNS.items():
            row = session.exec(text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'systems' AND column_name = :c"
            ), params={"c": name}).first()
            if row is None:
                problems.append(f"column {name} is missing")
                continue
            data_type, is_nullable = row
            if is_nullable != "YES":
                problems.append(f"{name}: expected nullable, got is_nullable={is_nullable}")
            want = "jsonb" if sql_type == "JSONB" else "text"
            if data_type != want:
                problems.append(f"{name}: expected {want}, got {data_type}")

        # The two revived columns must exist too — this migration assumes them.
        for name in ("faction_list", "icon_folder"):
            if session.exec(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'systems' AND column_name = :c"
            ), params={"c": name}).first() is None:
                problems.append(f"expected pre-existing column {name} to be present")

        # Every existing system must still be reading from its code module.
        # A non-NULL faction_list here before the staged migration has begun
        # would mean a system silently switched source without being tested.
        authored = session.exec(text(
            "SELECT name FROM systems WHERE faction_list IS NOT NULL ORDER BY name"
        )).all()
        print(f"  systems now authored in the DB: {[a[0] for a in authored] or 'none'}")
        print(f"  systems still reading systems/*.py: "
              f"{session.exec(text('SELECT count(*) FROM systems WHERE faction_list IS NULL')).first()[0]}")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        add_columns()
    issues = verify()
    if issues:
        print("VERIFY FAILED:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("Verified: systems.faction_groups / logo_path / logo_url are nullable.")
