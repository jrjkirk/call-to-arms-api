"""Phase 3, step 1: create the `club_webhooks` table and seed Manchester's
current per-webhook Discord URLs from the env vars the six existing call
sites already read.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Run manually:

    python seed_club_webhooks.py            # create + seed + verify
    python seed_club_webhooks.py --verify-only

Safe to re-run: table creation is idempotent (CREATE TABLE IF NOT EXISTS via
SQLModel's checkfirst), and seeding is an upsert keyed on
(club_id, webhook_type, system_id) — no DB-level unique constraint, same
check-then-upsert pattern as seed_clubs.py's ClubSystem seeding (see
ClubWebhook's docstring in models.py for why a plain UNIQUE constraint
would silently fail to enforce this for the three club-level types).

This is expand-only: nothing reads from club_webhooks yet. All six call
sites (signups.py, post_pairings_image.py, run_call_to_arms.py,
run_hh_call_to_arms.py, run_kt_call_to_arms.py, league.py,
post_league_rankings_image.py, services.py) keep reading their env vars
exactly as today.

Security: webhook URLs are secrets. This script never prints, logs, or
returns the actual URL values anywhere — only presence/absence, row counts,
and which env var names (not values) were empty/skipped.

If an expected env var is empty/unset, no row is created for it at all —
an empty url="" row would be indistinguishable from "configured with an
empty string".
"""
import os
import sys

from sqlmodel import Session, select

from database import engine
from models import Club, ClubWebhook, SystemConfig

CLUB_SLUG = "manchester"

# (webhook_type, legacy_system_name, env_var_name). legacy_system_name is
# None for the three club-level webhook types (system_id always NULL for
# those). Mirrors the six real call sites exactly — see PROJECT_STATUS.md.
SEED_CLUB_WEBHOOKS = [
    ("signup", "The Old World", "DISCORD_SIGNUP_WEBHOOK_URL"),
    ("signup", "The Horus Heresy", "DISCORD_HH_SIGNUP_WEBHOOK_URL"),
    ("signup", "Kill Team", "DISCORD_KT_SIGNUP_WEBHOOK_URL"),
    ("pairings", "The Old World", "DISCORD_TOW_PAIRINGS_WEBHOOK_URL"),
    ("pairings", "The Horus Heresy", "DISCORD_HH_PAIRINGS_WEBHOOK_URL"),
    ("pairings", "Kill Team", "DISCORD_KT_PAIRINGS_WEBHOOK_URL"),
    ("call_to_arms", "The Old World", "DISCORD_CALL_TO_ARMS_WEBHOOK_URL"),
    ("call_to_arms", "The Horus Heresy", "DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL"),
    ("call_to_arms", "Kill Team", "DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL"),
    ("league_result", None, "DISCORD_LEAGUE_RESULT_WEBHOOK_URL"),
    ("league_rankings", None, "DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL"),
    ("achievement", None, "DISCORD_ACHIEVEMENT_WEBHOOK_URL"),
]


def create_table():
    ClubWebhook.metadata.create_all(engine, tables=[ClubWebhook.__table__], checkfirst=True)


def _system_ids_by_legacy_name(session: Session) -> dict[str, int]:
    rows = session.exec(select(SystemConfig)).all()
    return {r.legacy_system_name: r.id for r in rows}


def _row_label(webhook_type: str, legacy_system_name: str | None) -> str:
    return f"{webhook_type}/{legacy_system_name}" if legacy_system_name else webhook_type


def _expected_rows(session: Session) -> list[dict]:
    """Resolve system_id + read the live env var for each of the 12
    combinations. Rebuilt fresh each call so seed() and verify() always
    see the current environment, not a stale cached copy."""
    system_ids = _system_ids_by_legacy_name(session)
    expected = []
    for webhook_type, legacy_system_name, env_var_name in SEED_CLUB_WEBHOOKS:
        system_id = system_ids[legacy_system_name] if legacy_system_name else None
        url = os.environ.get(env_var_name, "")
        expected.append(
            dict(
                webhook_type=webhook_type,
                legacy_system_name=legacy_system_name,
                system_id=system_id,
                env_var_name=env_var_name,
                url=url,
            )
        )
    return expected


def seed(session: Session) -> tuple[int, int, list[str]]:
    """Returns (seeded_count, skipped_count, skipped_env_var_names)."""
    club = session.exec(select(Club).where(Club.slug == CLUB_SLUG)).first()
    if club is None:
        raise RuntimeError(f"No seeded club with slug={CLUB_SLUG!r} — run seed_clubs.py first.")

    expected = _expected_rows(session)

    seeded = 0
    skipped = 0
    skipped_env_vars = []

    for row in expected:
        if not row["url"]:
            skipped += 1
            skipped_env_vars.append(row["env_var_name"])
            continue

        existing = session.exec(
            select(ClubWebhook).where(
                ClubWebhook.club_id == club.id,
                ClubWebhook.webhook_type == row["webhook_type"],
                ClubWebhook.system_id == row["system_id"],
            )
        ).first()

        if existing:
            existing.url = row["url"]
            session.add(existing)
        else:
            session.add(
                ClubWebhook(
                    club_id=club.id,
                    webhook_type=row["webhook_type"],
                    system_id=row["system_id"],
                    url=row["url"],
                )
            )
        seeded += 1

    session.commit()
    return seeded, skipped, skipped_env_vars


def verify(session: Session) -> list[str]:
    """Diff seeded rows against the DB and against the live env vars they're
    supposed to mirror. Never includes an actual URL value in a problem
    description — only presence/absence and row counts."""
    problems: list[str] = []

    club = session.exec(select(Club).where(Club.slug == CLUB_SLUG)).first()
    if club is None:
        problems.append(f"Missing seeded club slug={CLUB_SLUG!r}")
        return problems

    expected = _expected_rows(session)
    expected_with_url = [r for r in expected if r["url"]]

    cw_rows = session.exec(select(ClubWebhook).where(ClubWebhook.club_id == club.id)).all()
    if len(cw_rows) != len(expected_with_url):
        problems.append(
            f"ClubWebhook row count mismatch: db has {len(cw_rows)}, "
            f"expected {len(expected_with_url)} (env vars currently set)"
        )

    cw_by_key = {(r.webhook_type, r.system_id): r for r in cw_rows}

    for row in expected_with_url:
        key = (row["webhook_type"], row["system_id"])
        label = _row_label(row["webhook_type"], row["legacy_system_name"])
        actual = cw_by_key.get(key)
        if actual is None:
            problems.append(f"[{label}] missing seeded row (env var {row['env_var_name']} is set)")
            continue
        if actual.url != row["url"]:
            problems.append(
                f"[{label}] url does not match live env var {row['env_var_name']} (values not shown)"
            )

    for row in expected:
        if row["url"]:
            continue
        key = (row["webhook_type"], row["system_id"])
        label = _row_label(row["webhook_type"], row["legacy_system_name"])
        if key in cw_by_key:
            problems.append(
                f"[{label}] unexpected row exists but env var {row['env_var_name']} is now empty/unset"
            )

    return problems


def main():
    verify_only = "--verify-only" in sys.argv

    if not verify_only:
        print("Creating club_webhooks table (idempotent)...")
        create_table()
        with Session(engine) as session:
            print("Seeding Manchester's club webhooks from live env vars...")
            seeded, skipped, skipped_env_vars = seed(session)
            print(f"Seeded {seeded} row(s), skipped {skipped} (env var empty/unset).")
            if skipped_env_vars:
                print("Skipped env vars (names only, not values):")
                for name in skipped_env_vars:
                    print(f"  - {name}")

    with Session(engine) as session:
        problems = verify(session)

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: seeded rows match live env vars (12 expected combinations).")


if __name__ == "__main__":
    main()
