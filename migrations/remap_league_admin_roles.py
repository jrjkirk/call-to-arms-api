"""Modular leagues, scope retirement: remap any admin_roles rows with the old
standalone scope="League" to that club's real league-enabled system's scope
name (e.g. "The Old World"). Must run BEFORE deploying the code that removes
"League" from valid_scopes() — otherwise those grants become permanently
unusable (not just relabeled) since the scope name would no longer validate.

Idempotent: skips a club with no scope="League" rows; de-dupes (deletes the
old row instead of renaming it) if the user already holds the target scope.

Run (staging then prod):
    PYTHONPATH=. python migrations/remap_league_admin_roles.py
    PYTHONPATH=. python migrations/remap_league_admin_roles.py --verify-only
"""
import sys

from sqlmodel import Session, select

from database import engine
from models import AdminRole, ClubSystem, SystemConfig


def _club_league_system_name(db: Session, club_id: int) -> str | None:
    row = db.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club_id, ClubSystem.league_enabled == True)
    ).first()
    return row[1].legacy_system_name if row else None


def run():
    with Session(engine) as db:
        rows = db.exec(select(AdminRole).where(AdminRole.scope == "League")).all()
        if not rows:
            print("No admin_roles rows with scope='League'. Nothing to do.")
            return

        remapped = deleted = skipped = 0
        for row in rows:
            target = _club_league_system_name(db, row.club_id)
            if target is None:
                print(f"  SKIP id={row.id} club={row.club_id} user={row.user_id}: "
                      f"club has no league-enabled system to remap to.")
                skipped += 1
                continue
            existing = db.exec(
                select(AdminRole).where(
                    AdminRole.club_id == row.club_id,
                    AdminRole.user_id == row.user_id,
                    AdminRole.scope == target,
                )
            ).first()
            if existing is not None:
                db.delete(row)
                deleted += 1
                print(f"  DEDUP id={row.id} club={row.club_id} user={row.user_id}: "
                      f"already holds {target!r}, deleted the League row.")
            else:
                row.scope = target
                db.add(row)
                remapped += 1
                print(f"  REMAP id={row.id} club={row.club_id} user={row.user_id}: "
                      f"League -> {target!r}.")
        db.commit()
        print(f"\nDone: {remapped} remapped, {deleted} de-duped, {skipped} skipped.")


def verify():
    with Session(engine) as db:
        remaining = db.exec(select(AdminRole).where(AdminRole.scope == "League")).all()
    if remaining:
        print(f"VERIFICATION FAILED: {len(remaining)} admin_roles row(s) still scope='League'.")
        for r in remaining:
            print(f"  - id={r.id} club={r.club_id} user={r.user_id}")
        sys.exit(1)
    print("Verification passed: no admin_roles rows with scope='League' remain.")


def main():
    if "--verify-only" not in sys.argv:
        run()
    verify()


if __name__ == "__main__":
    main()
