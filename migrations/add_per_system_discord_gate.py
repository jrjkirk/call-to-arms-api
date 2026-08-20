"""Per-(club, system) Discord gate (2026-08-20): expand step taking the
"must be in the Discord to sign up" check from one server per CLUB to one
server per CLUB NIGHT.

Why: a club's game nights can each run out of a different Discord server —
at EGNWGC, Kill Team and The Old World are separate servers, neither of them
a club-wide one. `clubs.discord_guild_id` can only ever name one of them, so
the gate could never be right for both.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). `club_systems` already
exists with real rows, so SQLModel's create_all() won't alter it; the
ALTER TABLEs run by hand here.

Added:
  - club_systems.discord_gate_enabled -> BOOLEAN NOT NULL DEFAULT FALSE. The
                                       per-system opt-in. FALSE (every existing
                                       row) means that game night is never
                                       gated, whatever the club-level settings
                                       say. No inheritance by design.
  - club_systems.discord_guild_id   -> this system's Discord server snowflake.
                                       NULL -> fall back to clubs.discord_guild_id,
                                       for the common club-with-one-server case.
  - club_systems.discord_url        -> this system's invite link, shown as the
                                       "Join our Discord" button on its Club-page
                                       carousel card AND in the gate refusal.
                                       NULL -> fall back to clubs.discord_url.
  - club_systems.discord_gate_mode  -> 'monitor' | 'enforce' within an opted-in
                                       system. NULL -> 'monitor'.

Created:
  - player_discord_verifications    -> the per-(player, guild) membership cache
                                       that replaces players.discord_verified_at
                                       as the thing consulted going forward.

DELIBERATELY NO BACKFILL. Every existing row lands on
discord_gate_enabled=FALSE with the other three NULL, so this migration
changes no behaviour at all: nothing is gated that wasn't, and each club's
existing Discord link keeps being what its systems inherit. Copying
clubs.discord_guild_id down into each system would be actively wrong — it
would assert "this system's server is the club server" for exactly the
systems this change exists to separate.

Note this migration OPTS EVERY SYSTEM OUT, including any club that had the
old club-wide gate switched on. No club does today (every mode is 'off' and
no club has a guild id), so nothing regresses — but if that ever stops being
true, re-opt those systems in by hand rather than backfilling TRUE.

players.discord_verified_at IS LEFT IN PLACE and keeps its meaning as a
blanket grandfather flag: the 100+ players stamped by
add_discord_guild_gate.py --grandfather stay exempt from every system's gate,
which is exactly what that stamp was for. New verifications are recorded
per-guild in the new table instead.

Run manually, in order:

    PYTHONPATH=. python migrations/add_per_system_discord_gate.py --add-columns
    PYTHONPATH=. python migrations/add_per_system_discord_gate.py --verify

And again against PRODUCTION — the local .env DATABASE_URL is the STAGING
Supabase project, so running this locally does NOT touch prod:

    fly ssh console -C "sh -c 'cd /app && PYTHONPATH=. python migrations/add_per_system_discord_gate.py --add-columns'"

DEPLOY ORDER MATTERS. select(ClubSystem) will include the new columns as soon
as models.py lands on main, and the scheduled GitHub Actions crons run from
main against the PROD database — so prod breaks at PUSH time, before any
`fly deploy`. Always: prod migration -> push -> deploy.

Safe to re-run: ALTER TABLE / CREATE TABLE both use IF NOT EXISTS.
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import Club, ClubSystem, SystemConfig


def add_columns():
    with engine.begin() as conn:
        for col, coltype in (
            ("discord_guild_id", "VARCHAR"),
            ("discord_url", "VARCHAR"),
            ("discord_gate_mode", "VARCHAR"),
            # NOT NULL DEFAULT FALSE, not nullable: "has this system opted in"
            # is a yes/no with a safe default, and a NULL third state would
            # only invite the inheritance this design deliberately refuses.
            ("discord_gate_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ):
            conn.execute(text(
                f"ALTER TABLE club_systems ADD COLUMN IF NOT EXISTS {col} {coltype}"
            ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS player_discord_verifications (
                id SERIAL PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES players(id),
                guild_id VARCHAR NOT NULL,
                verified_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        # Indexed on the exact lookup the gate does on every uncached signup
        # ("is THIS player verified for THIS guild"), and unique so a race
        # between two concurrent signups can't write the row twice. The insert
        # in require_discord_member is check-then-insert, which is not atomic.
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_pdv_player_guild "
            "ON player_discord_verifications (player_id, guild_id)"
        ))
    print("Added club_systems.discord_{guild_id,url,gate_mode} and "
          "player_discord_verifications (or already present).")


def verify():
    with Session(engine) as session:
        rows = session.exec(
            select(ClubSystem, SystemConfig, Club)
            .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
            .join(Club, Club.id == ClubSystem.club_id)
            .order_by(Club.slug, SystemConfig.name)
        ).all()

        count = session.exec(text(
            "SELECT COUNT(*) FROM player_discord_verifications"
        )).one()
        print(f"player_discord_verifications rows: {count[0] if isinstance(count, tuple) else count}")

        print(f"club_systems: {len(rows)}")
        opted_in = 0
        for cs, sc, club in rows:
            guild = cs.discord_guild_id or f"(inherits club: {club.discord_guild_id or 'none'})"
            invite = cs.discord_url or f"(inherits club: {club.discord_url or 'none'})"
            if cs.discord_gate_enabled:
                opted_in += 1
                state = f"ON ({cs.discord_gate_mode or 'monitor'})"
            else:
                state = "opted out"
            print(f"  - {club.slug} / {sc.name}: gate={state}")
            print(f"      guild={guild}")
            print(f"      invite={invite}")

        print(f"\nsystems opted in: {opted_in}/{len(rows)}")
        if opted_in == 0:
            print("Every system is opted out — this migration changed no behaviour, "
                  "which is the expected state right after it runs.")
        print("OK")
        return True


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args & {"--add-columns", "--verify"}:
        print(__doc__)
        sys.exit(1)
    if "--add-columns" in args:
        add_columns()
    if "--verify" in args:
        verify()
