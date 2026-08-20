"""Level "ding" high-water marks (2026-08-20).

Levels themselves are DERIVED from pairings and need no storage or backfill —
existing players are already levelled by their history the moment the feature
ships, which is what "retroactively level up existing players" asks for.

This table exists only so an announcement fires once. Seeding it to everyone's
CURRENT level is the important part: without it, the first publish after
deploy would post a "ding" for every milestone every player has ever crossed,
which for a club with months of history is hundreds of messages.

    PYTHONPATH=. python migrations/add_player_level_announcements.py --create
    PYTHONPATH=. python migrations/add_player_level_announcements.py --seed
    PYTHONPATH=. python migrations/add_player_level_announcements.py --verify

Run --create AND --seed against production before deploying:

    fly ssh console -C "sh -c 'cd /app && PYTHONPATH=. python migrations/add_player_level_announcements.py --create --seed'"
"""
import sys

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import Player, PlayerLevelAnnouncement, ClubSystem, SystemConfig
import levels


def create():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS player_level_announcements (
                id SERIAL PRIMARY KEY,
                club_id INTEGER NOT NULL REFERENCES clubs(id),
                player_id INTEGER NOT NULL REFERENCES players(id),
                system VARCHAR NOT NULL,
                last_level INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_pla_club_player_system "
            "ON player_level_announcements (club_id, player_id, system)"
        ))
    print("Created player_level_announcements (or already present).")


def seed():
    """Mark every player as already announced at their current level, so the
    first publish after deploy celebrates only NEW progress."""
    with Session(engine) as db:
        rows = db.exec(
            select(ClubSystem, SystemConfig)
            .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        ).all()
        made = 0
        for cs, sc in rows:
            players = db.exec(
                select(Player).where(Player.club_id == cs.club_id).where(Player.active == True)
            ).all()
            for p in players:
                exists = db.exec(
                    select(PlayerLevelAnnouncement)
                    .where(PlayerLevelAnnouncement.club_id == cs.club_id)
                    .where(PlayerLevelAnnouncement.player_id == p.id)
                    .where(PlayerLevelAnnouncement.system == sc.legacy_system_name)
                ).first()
                if exists:
                    continue
                lv = levels.progress(db, cs.club_id, p.id, sc.legacy_system_name)["level"]
                db.add(PlayerLevelAnnouncement(
                    club_id=cs.club_id, player_id=p.id,
                    system=sc.legacy_system_name, last_level=lv,
                ))
                made += 1
        db.commit()
        print(f"Seeded {made} (player, system) high-water marks at their current level.")


def verify():
    with Session(engine) as db:
        rows = db.exec(select(PlayerLevelAnnouncement)).all()
        print(f"rows: {len(rows)}")
        by_level = {}
        for r in rows:
            by_level[r.last_level] = by_level.get(r.last_level, 0) + 1
        for lv in sorted(by_level, reverse=True)[:8]:
            print(f"   level {lv:3}: {by_level[lv]} (player, system) pairs")
        print("OK")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args & {"--create", "--seed", "--verify"}:
        print(__doc__)
        sys.exit(1)
    if "--create" in args:
        create()
    if "--seed" in args:
        seed()
    if "--verify" in args:
        verify()
