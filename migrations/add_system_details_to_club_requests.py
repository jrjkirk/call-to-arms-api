"""Add `system_details` JSON to `club_requests` (per-system club nights).

A request used to carry ONE club night for the whole club: `club_night_day`,
`club_night_time`, and a single `player_count`. Provisioning then stamped that
same night onto every ClubSystem it created, always weekly.

That is wrong for most real clubs. The Old World on a Thursday and Kill Team
fortnightly on a Tuesday is ordinary, and a club asked to give one answer gives
the one that is right for their biggest night and wrong for the rest — which
then goes out on their public page.

So the detail moves per system:

    {
      "The Old World": {"day": "Thursday", "time": "18:30",
                        "cadence": "weekly", "players": 20},
      "Kill Team":     {"day": "Tuesday", "time": "19:00",
                        "cadence": "fortnightly", "players": 8}
    }

Keyed on the legacy system name, which is what `systems` already stores and
what ClubSystem resolves against, so no lookup is needed to match them up.

**The old columns stay and are still read.** Every request submitted before
today has them and has NULL here, and provisioning falls back to them, so an
old pending request still provisions correctly. Do not drop them without
checking for pending rows first.

One-off script, not a long-lived migration tool (see CLAUDE.md / models.py
docstring). Additive + idempotent, so it is safe to re-run.

Run manually (from repo root, so repo-root imports resolve):

    PYTHONPATH=. python migrations/add_system_details_to_club_requests.py
    PYTHONPATH=. python migrations/add_system_details_to_club_requests.py --verify-only
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine


def add_column():
    with Session(engine) as session:
        # JSONB rather than JSON: it is the type `systems` already uses on this
        # table, and it is the one Postgres can index if this ever needs it.
        session.exec(text(
            "ALTER TABLE club_requests ADD COLUMN IF NOT EXISTS system_details JSONB"
        ))
        session.commit()
    print("Added club_requests.system_details (or it was already there).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        row = session.exec(text(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'club_requests' AND column_name = 'system_details'"
        )).first()
        if row is None:
            problems.append("column system_details is missing")
        else:
            data_type, is_nullable = row
            # Nullable permanently: NULL means "submitted before per-system
            # detail existed", and provisioning reads the old flat columns for
            # exactly those rows.
            if is_nullable != "YES":
                problems.append(f"expected nullable column, got is_nullable={is_nullable}")
            if data_type not in ("jsonb", "json"):
                problems.append(f"expected jsonb, got {data_type}")

        pending = session.exec(text(
            "SELECT count(*) FROM club_requests "
            "WHERE status = 'pending' AND system_details IS NULL"
        )).first()
        print(f"  pending requests still on the old flat columns: "
              f"{pending[0] if pending else 0}")
    return problems


if __name__ == "__main__":
    if "--verify-only" not in sys.argv:
        add_column()
    issues = verify()
    if issues:
        print("VERIFY FAILED:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("Verified: club_requests.system_details is nullable JSONB.")
