"""Add the identity and provisioning columns to club_requests.

Two changes land together (2026-09-06):

**Identity.** The "add my club" form was fully anonymous — any club name, any
location, an email nobody verified. Requesting now needs a Discord sign-in, so
`discord_id` / `discord_name` record who actually asked. `discord_id` rather
than a user id, because a brand-new organiser cannot have a User row yet:
`users.club_id` is NOT NULL and the club they want is the one that doesn't
exist. The sign-in stops at the signed `cta_pending_signup` cookie, and this
column carries that identity to provisioning, which mints the User and the Club
in one go.

**Detail.** Everything that used to be an email exchange before a club could be
set up — region, subdomain, systems, club night, size, the requester's role,
and a link showing they run the place — is captured on the form instead.

All columns are nullable: rows predating this exist and stay valid, and the
platform-admin view renders them as "not given" rather than breaking.

Idempotent — checks each column before adding, so re-running is a no-op.

    PYTHONPATH=. python migrations/add_identity_and_detail_to_club_requests.py
    PYTHONPATH=. python migrations/add_identity_and_detail_to_club_requests.py --dry-run
"""
import sys

from sqlalchemy import inspect, text

from database import engine

TABLE = "club_requests"

# JSON, not TEXT, for `systems`: it is a list and Postgres can index into it if
# we ever want "which systems are clubs asking for". SQLite accepts JSON too.
COLUMNS = [
    ("discord_id", "VARCHAR"),
    ("discord_name", "VARCHAR"),
    ("requester_user_id", "INTEGER"),
    ("region", "VARCHAR"),
    ("preferred_slug", "VARCHAR"),
    ("systems", "JSON"),
    ("club_night_day", "VARCHAR"),
    ("club_night_time", "VARCHAR"),
    ("player_count", "INTEGER"),
    ("requester_role", "VARCHAR"),
    ("evidence_url", "VARCHAR"),
]

INDEXES = [
    ("ix_club_requests_discord_id", "discord_id"),
    ("ix_club_requests_requester_user_id", "requester_user_id"),
]


def main(dry_run: bool = False) -> None:
    existing = {c["name"] for c in inspect(engine).get_columns(TABLE)}
    todo = [(n, t) for n, t in COLUMNS if n not in existing]

    if not todo:
        print(f"All {len(COLUMNS)} columns already present on {TABLE} — nothing to do.")
    for name, coltype in todo:
        print(f"  ADD COLUMN {name} {coltype}")

    if dry_run:
        print(f"\nDRY RUN — would add {len(todo)} column(s).")
        return

    with engine.begin() as conn:
        for name, coltype in todo:
            conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {coltype}"))
        for index_name, column in INDEXES:
            if column in existing and not todo:
                continue
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {TABLE} ({column})")
            )

    after = {c["name"] for c in inspect(engine).get_columns(TABLE)}
    missing = [n for n, _ in COLUMNS if n not in after]
    if missing:
        print(f"\nFAILED — still missing: {missing}")
        raise SystemExit(1)
    print(f"\nAdded {len(todo)} column(s); all {len(COLUMNS)} now present on {TABLE}.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
