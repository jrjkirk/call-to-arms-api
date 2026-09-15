"""Merge two accounts into one (account overhaul Slab 1).

One person, two User rows: two Discord accounts (Shaun), or later a Discord
account and a Google one that turn out to be the same human. Everything that
points at the account being dropped moves to the account being kept, and the
dropped row is deleted.

Two ways in:
  * by hand, through migrations/merge_users.py (dry run by default)
  * from linking (Slab 6), once someone has proved they hold both identities

Refuses rather than guesses. Before anything is written, plan_merge looks for
the things that can only exist once and would collide:

  * both own a player at the same club (one player per club; merge the
    players first, see migrations/merge_duplicate_players.py)
  * both are entered in the same tournament
  * both are super-admin of different clubs (there is no multi-club admin)

and for any user-id column this module has never heard of. USER_REFERENCES is
checked against the live table metadata, so a column added next month makes
the merge refuse instead of silently leaving rows pointing at a deleted user.

Imports models only, never database or auth.
"""
from dataclasses import dataclass, field

from sqlmodel import SQLModel, Session, select

from models import (
    AdminRole, AuditLogEntry, ClubRequest, Player, Tournament, TournamentEntry,
    TournamentGame, User, UserIdentity, VenueBooking, VenueEvent, VenueStaff,
)

# Every column that holds a users.id, and how a merge treats it.
#   move          re-point at the kept account
#   identity      move; stops being primary if the kept account already has
#                 a primary identity from that provider (it still signs in)
#   player        move; refused if both own a player at the club (plan_merge)
#   entry         move; refused if both are in the tournament (plan_merge)
#   dedupe:a,b    move, unless the kept account already has a row with the
#                 same values of a,b, in which case the dropped row is deleted
USER_REFERENCES: list[tuple[type[SQLModel], str, str]] = [
    (UserIdentity, "user_id", "identity"),
    (Player, "user_id", "player"),
    (AdminRole, "user_id", "dedupe:club_id,scope"),
    (VenueStaff, "user_id", "dedupe:club_id"),
    (TournamentEntry, "user_id", "entry"),
    (VenueBooking, "user_id", "move"),
    (VenueBooking, "cancelled_by_user_id", "move"),
    (Tournament, "created_by_user_id", "move"),
    (TournamentGame, "reported_by_user_id", "move"),
    (VenueEvent, "approved_by_user_id", "move"),
    (VenueEvent, "created_by_user_id", "move"),
    # The actor is the same person; actor_name stays as the snapshot it was.
    (AuditLogEntry, "actor_user_id", "move"),
    (ClubRequest, "requester_user_id", "move"),
    (ClubRequest, "reviewed_by_user_id", "move"),
]

# Columns that look like user references by name but aren't.
NOT_USER_REFERENCES = {("user_identities", "provider_user_id")}


def unhandled_user_columns() -> list[str]:
    """Columns in the schema that hold a user id and aren't in USER_REFERENCES.

    A column counts if it has a foreign key to users.id or its name ends in
    user_id. Empty means the merge knows about everything.
    """
    handled = {(m.__tablename__, c) for m, c, _ in USER_REFERENCES}
    found = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name == "users":
            continue
        for col in table.columns:
            refs_users = any(fk.column.table.name == "users" for fk in col.foreign_keys)
            if not (refs_users or col.name.endswith("user_id")):
                continue
            key = (table.name, col.name)
            if key not in handled and key not in NOT_USER_REFERENCES:
                found.append(f"{table.name}.{col.name}")
    return found


class MergeRefused(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class MergePlan:
    keep: User
    drop: User
    problems: list[str] = field(default_factory=list)


def plan_merge(db: Session, keep_id: int, drop_id: int) -> MergePlan:
    """Check a merge without writing anything."""
    keep = db.get(User, keep_id)
    drop = db.get(User, drop_id)
    plan = MergePlan(keep=keep, drop=drop)
    if keep is None or drop is None:
        plan.problems.append(f"user {keep_id if keep is None else drop_id} does not exist")
        return plan
    if keep.id == drop.id:
        plan.problems.append("an account can't be merged into itself")
        return plan

    unknown = unhandled_user_columns()
    if unknown:
        plan.problems.append(
            "user_merge.USER_REFERENCES doesn't cover " + ", ".join(unknown)
            + "; add them before merging, or rows will point at a deleted user"
        )

    def rows(model, col, uid):
        return db.exec(select(model).where(getattr(model, col) == uid)).all()

    keep_players = {p.club_id: p for p in rows(Player, "user_id", keep.id)}
    for p in rows(Player, "user_id", drop.id):
        other = keep_players.get(p.club_id)
        if other is not None:
            plan.problems.append(
                f"both own a player at club {p.club_id} ({other.name!r} #{other.id} and "
                f"{p.name!r} #{p.id}); merge those players first"
            )

    keep_entries = {e.tournament_id for e in rows(TournamentEntry, "user_id", keep.id)}
    for e in rows(TournamentEntry, "user_id", drop.id):
        if e.tournament_id in keep_entries:
            plan.problems.append(f"both are entered in tournament {e.tournament_id}")

    if (keep.is_super_admin and drop.is_super_admin and keep.club_id != drop.club_id):
        plan.problems.append(
            f"both are super-admin, of clubs {keep.club_id} and {drop.club_id}; "
            "one person can't own two clubs"
        )
    return plan


def merge_users(db: Session, keep_id: int, drop_id: int) -> list[str]:
    """Move everything from `drop_id` onto `keep_id` and delete `drop_id`.

    Caller commits (or rolls back for a dry run). Returns a line per change.
    Raises MergeRefused, having written nothing, if plan_merge finds problems.
    """
    plan = plan_merge(db, keep_id, drop_id)
    if plan.problems:
        raise MergeRefused(plan.problems)
    keep, drop = plan.keep, plan.drop
    log: list[str] = []

    # The Discord mirror is UNIQUE, so it has to leave the dropped row before it
    # can arrive on the kept one.
    drop_discord = (drop.discord_id, drop.discord_name, drop.avatar_url)
    if drop.discord_id:
        drop.discord_id = None
        db.add(drop)
        db.flush()

    def keep_primary(provider: str) -> bool:
        return db.exec(
            select(UserIdentity).where(UserIdentity.user_id == keep.id)
            .where(UserIdentity.provider == provider)
            .where(UserIdentity.is_primary == True)  # noqa: E712
        ).first() is not None

    for model, col, how in USER_REFERENCES:
        attr = getattr(model, col)
        for row in db.exec(select(model).where(attr == drop.id)).all():
            label = f"{model.__tablename__} #{row.id}.{col}"
            if how.startswith("dedupe:"):
                keys = how.split(":", 1)[1].split(",")
                same = [
                    getattr(model, k).is_(None) if getattr(row, k) is None
                    else getattr(model, k) == getattr(row, k)
                    for k in keys
                ]
                clash = db.exec(select(model).where(attr == keep.id).where(*same)).first()
                if clash is not None:
                    db.delete(row)
                    log.append(f"{label}: kept account already has it, dropped")
                    continue
            if how == "identity" and row.is_primary and keep_primary(row.provider):
                # Both accounts' ways in keep working; the kept account's
                # existing one stays the one that gets mentioned.
                row.is_primary = False
                log.append(f"{label}: {row.provider} {row.provider_user_id} joins as a secondary identity")
            setattr(row, col, keep.id)
            db.add(row)
            log.append(f"{label} -> {keep.id}")

    if drop_discord[0] and not keep.discord_id:
        keep.discord_id, keep.discord_name, keep.avatar_url = drop_discord
        log.append(f"users #{keep.id}: takes Discord {drop_discord[1]!r} ({drop_discord[0]})")

    if drop.is_super_admin and not keep.is_super_admin:
        # Super-admin authority is is_super_admin AND club_id == that club, so
        # the club has to come with the flag.
        keep.is_super_admin = True
        keep.club_id = drop.club_id
        log.append(f"users #{keep.id}: super-admin of club {drop.club_id}")
    if keep.club_id is None:
        keep.club_id = drop.club_id

    if keep.player_id is None and drop.player_id is not None:
        # The legacy home-club back-link, dual-written only for the home club
        # (see auth.claim_player). Take it if that player now belongs to keep
        # and sits at keep's club.
        p = db.get(Player, drop.player_id)
        if p is not None and p.user_id == keep.id and p.club_id == keep.club_id:
            keep.player_id = p.id
            log.append(f"users #{keep.id}.player_id -> {p.id}")

    if not keep.display_name and drop.display_name:
        keep.display_name = drop.display_name
        log.append(f"users #{keep.id}.display_name -> {drop.display_name!r}")
    if drop.is_platform_admin and not keep.is_platform_admin:
        keep.is_platform_admin = True
        log.append(f"users #{keep.id}: platform admin")
    if keep.home_club_id is None:
        keep.home_club_id = drop.home_club_id
    if drop.created_at and (keep.created_at is None or drop.created_at < keep.created_at):
        keep.created_at = drop.created_at
    if drop.last_login_at and (keep.last_login_at is None or drop.last_login_at > keep.last_login_at):
        keep.last_login_at = drop.last_login_at
    db.add(keep)

    db.flush()
    db.delete(drop)
    db.flush()
    log.append(f"users #{drop.id} deleted")
    return log
