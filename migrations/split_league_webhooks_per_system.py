"""League webhooks going per-system: migrate existing club-level
(system_id=NULL) league_result/achievement/league_rankings ClubWebhook rows
to point at that club's single league-enabled system.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). No schema change needed —
ClubWebhook.system_id already exists and is nullable; this only updates
existing rows' values so the new per-system-only lookup
(resolve_webhook_url(db, club_id, webhook_type, system_id)) can find them.

Only handles the unambiguous case: a club with exactly one league-enabled
ClubSystem. Skips (and reports) any club with zero or more-than-one
league-enabled system for these row types — not expected today (no club runs
two leagues yet) but the script refuses to guess rather than silently
mis-assign a real webhook URL.

Run (staging then prod):
    PYTHONPATH=. python migrations/split_league_webhooks_per_system.py
    PYTHONPATH=. python migrations/split_league_webhooks_per_system.py --verify-only
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import ClubSystem, ClubWebhook

LEAGUE_WEBHOOK_TYPES = ("league_result", "achievement", "league_rankings")


def _single_league_system_id(db: Session, club_id: int) -> int | None:
    rows = db.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == club_id, ClubSystem.league_enabled == True
        )
    ).all()
    return rows[0].system_id if len(rows) == 1 else None


def run():
    with Session(engine) as db:
        rows = db.exec(
            select(ClubWebhook).where(
                ClubWebhook.webhook_type.in_(LEAGUE_WEBHOOK_TYPES),
                ClubWebhook.system_id.is_(None),
            )
        ).all()
        if not rows:
            print("No club-level league/achievement webhook rows found. Nothing to do.")
            return

        migrated = skipped = 0
        for row in rows:
            system_id = _single_league_system_id(db, row.club_id)
            if system_id is None:
                print(f"  SKIP id={row.id} club={row.club_id} type={row.webhook_type!r}: "
                      f"club has zero or multiple league-enabled systems — resolve manually.")
                skipped += 1
                continue
            print(f"  MIGRATE id={row.id} club={row.club_id} type={row.webhook_type!r}: "
                  f"system_id NULL -> {system_id}")
            row.system_id = system_id
            db.add(row)
            migrated += 1
        db.commit()
        print(f"\nDone: {migrated} migrated, {skipped} skipped.")


def verify():
    with Session(engine) as db:
        remaining = db.exec(
            select(ClubWebhook).where(
                ClubWebhook.webhook_type.in_(LEAGUE_WEBHOOK_TYPES),
                ClubWebhook.system_id.is_(None),
            )
        ).all()
    if remaining:
        print(f"{len(remaining)} club-level league/achievement webhook row(s) remain "
              f"(expected only for clubs with zero or multiple league-enabled systems):")
        for r in remaining:
            print(f"  - id={r.id} club={r.club_id} type={r.webhook_type!r}")
    else:
        print("No club-level league/achievement webhook rows remain — all migrated.")


def main():
    if "--verify-only" not in sys.argv:
        run()
    verify()


if __name__ == "__main__":
    main()
