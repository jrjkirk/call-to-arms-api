"""Phase 1, step 1: create the `clubs` and `club_systems` tables and seed the
current single club (Manchester) and its per-system schedule.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Run manually:

    python seed_clubs.py            # create + seed + verify
    python seed_clubs.py --verify-only

Safe to re-run: table creation is idempotent (CREATE TABLE IF NOT EXISTS via
SQLModel's checkfirst), and seeding is an upsert keyed on `slug` for Club and
`club_id`+`system_id` for ClubSystem.

The verification step reads back what's on staging and confirms it against
the actual live scheduling logic (week_logic.py's weekday math and
HH_SESSION_ANCHOR below) rather than just trusting the literals typed into
SEED_CLUB_SYSTEMS below.
"""
import sys
from datetime import date

from sqlmodel import Session, select

from database import engine
from models import Club, ClubSystem, SystemConfig

# Formerly imported from run_hh_call_to_arms.py (deleted along with the other
# two per-system manual-fallback scripts — superseded by call-to-arms-check.yml,
# see run_call_to_arms_check.py). This is the one remaining consumer.
HH_SESSION_ANCHOR = date(2026, 5, 8)

CLUB = dict(
    name="Manchester",
    slug="manchester",
    active=True,
    timezone="Europe/London",
    contact_email=None,
    leagues_enabled=True,
)

# system_id is resolved at seed/verify time by looking up SystemConfig.legacy_system_name.
SEED_CLUB_SYSTEMS = [
    dict(
        legacy_system_name="The Old World",
        enabled=True,
        session_day="Wednesday",
        session_cadence="weekly",
        cadence_anchor=None,
    ),
    dict(
        legacy_system_name="Kill Team",
        enabled=True,
        session_day="Friday",
        session_cadence="weekly",
        cadence_anchor=None,
    ),
    dict(
        legacy_system_name="The Horus Heresy",
        enabled=True,
        session_day="Friday",
        session_cadence="fortnightly",
        cadence_anchor=HH_SESSION_ANCHOR,
    ),
]


def create_tables():
    Club.metadata.create_all(engine, tables=[Club.__table__], checkfirst=True)
    ClubSystem.metadata.create_all(engine, tables=[ClubSystem.__table__], checkfirst=True)


def _system_ids_by_legacy_name(session: Session) -> dict[str, int]:
    rows = session.exec(select(SystemConfig)).all()
    return {r.legacy_system_name: r.id for r in rows}


def seed(session: Session):
    existing_club = session.exec(select(Club).where(Club.slug == CLUB["slug"])).first()
    if existing_club:
        for k, v in CLUB.items():
            setattr(existing_club, k, v)
        session.add(existing_club)
        session.flush()
        club = existing_club
    else:
        club = Club(**CLUB)
        session.add(club)
        session.flush()

    system_ids = _system_ids_by_legacy_name(session)

    for row in SEED_CLUB_SYSTEMS:
        system_id = system_ids[row["legacy_system_name"]]
        existing = session.exec(
            select(ClubSystem).where(
                ClubSystem.club_id == club.id,
                ClubSystem.system_id == system_id,
            )
        ).first()
        fields = dict(
            club_id=club.id,
            system_id=system_id,
            enabled=row["enabled"],
            session_day=row["session_day"],
            session_cadence=row["session_cadence"],
            cadence_anchor=row["cadence_anchor"],
        )
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
            session.add(existing)
        else:
            session.add(ClubSystem(**fields))

    session.commit()


def verify(session: Session) -> list[str]:
    """Diff seeded rows against the DB and against the live scheduling logic
    they're supposed to mirror. Returns a list of mismatch descriptions;
    empty list means clean."""
    problems: list[str] = []

    club = session.exec(select(Club).where(Club.slug == CLUB["slug"])).first()
    if club is None:
        problems.append(f"Missing seeded club slug={CLUB['slug']}")
        return problems

    for field, expected_val in CLUB.items():
        actual_val = getattr(club, field)
        if actual_val != expected_val:
            problems.append(f"[club] {field}: db={actual_val!r} expected={expected_val!r}")

    system_ids = _system_ids_by_legacy_name(session)

    cs_rows = session.exec(select(ClubSystem).where(ClubSystem.club_id == club.id)).all()
    if len(cs_rows) != len(SEED_CLUB_SYSTEMS):
        problems.append(
            f"ClubSystem row count mismatch: db has {len(cs_rows)}, expected {len(SEED_CLUB_SYSTEMS)}"
        )

    cs_by_system_id = {r.system_id: r for r in cs_rows}

    for row in SEED_CLUB_SYSTEMS:
        legacy_name = row["legacy_system_name"]
        system_id = system_ids.get(legacy_name)
        if system_id is None:
            problems.append(f"No SystemConfig row for legacy_system_name={legacy_name!r}")
            continue
        actual = cs_by_system_id.get(system_id)
        if actual is None:
            problems.append(f"Missing seeded ClubSystem row for {legacy_name}")
            continue
        for field in ("enabled", "session_day", "session_cadence", "cadence_anchor"):
            actual_val = getattr(actual, field)
            expected_val = row[field]
            if actual_val != expected_val:
                problems.append(
                    f"[{legacy_name}] {field}: db={actual_val!r} expected={expected_val!r}"
                )

    # Cross-check against the live source of truth, not just internally
    # consistent literals: week_logic.py's weekday math and this module's
    # own HH_SESSION_ANCHOR.
    tow_row = next((r for r in SEED_CLUB_SYSTEMS if r["legacy_system_name"] == "The Old World"), None)
    if tow_row and tow_row["session_day"] != "Wednesday":
        problems.append(
            f"TOW session_day={tow_row['session_day']!r} does not match week_logic._week_id_wed's "
            f"Wednesday (weekday==2) logic"
        )

    kt_row = next((r for r in SEED_CLUB_SYSTEMS if r["legacy_system_name"] == "Kill Team"), None)
    if kt_row and kt_row["session_day"] != "Friday":
        problems.append(
            f"KT session_day={kt_row['session_day']!r} does not match week_logic._week_id_fri's "
            f"Friday (weekday==4) logic"
        )

    hh_row = next((r for r in SEED_CLUB_SYSTEMS if r["legacy_system_name"] == "The Horus Heresy"), None)
    if hh_row:
        if hh_row["session_day"] != "Friday":
            problems.append(f"HH session_day={hh_row['session_day']!r} does not match Friday")
        if hh_row["session_cadence"] != "fortnightly":
            problems.append(f"HH session_cadence={hh_row['session_cadence']!r} does not match fortnightly")
        if hh_row["cadence_anchor"] != HH_SESSION_ANCHOR:
            problems.append(
                f"HH cadence_anchor={hh_row['cadence_anchor']!r} does not match "
                f"HH_SESSION_ANCHOR={HH_SESSION_ANCHOR!r}"
            )

    return problems


def main():
    verify_only = "--verify-only" in sys.argv
    if not verify_only:
        print("Creating clubs + club_systems tables (idempotent)...")
        create_tables()
        with Session(engine) as session:
            print("Seeding Manchester club + its system schedules...")
            seed(session)

    with Session(engine) as session:
        problems = verify(session)

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: seeded rows match live scheduling logic.")


if __name__ == "__main__":
    main()
