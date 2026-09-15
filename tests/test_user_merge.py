"""Account overhaul Slab 1: merging two accounts into one.

Shaun is the case this is built for: an old Discord account holding his player
and history, and the Discord account he actually signs in with now, which owns
nothing. After a merge, both Discord accounts sign in to one account that has
everything, and the one he uses now is the one that gets mentioned.

What these assert:
  * every row pointing at the dropped account ends up on the kept one
  * both Discord accounts keep signing in; the kept one stays primary
  * duplicate grants collapse instead of doubling
  * a merge that would collide refuses, and writes nothing
  * a user-id column the merge doesn't know about makes it refuse

Run: PYTHONPATH=. python tests/test_user_merge.py
"""
import datetime as dt
import os
import pathlib
import subprocess
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "merge.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ.setdefault("SESSION_SECRET", "test-secret-for-merge")

from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
import user_merge  # noqa: E402
from identity import ProviderProfile, attach_identity, discord_ids_for_users, find_user_for_profile  # noqa: E402
from models import (  # noqa: E402
    AdminRole, AuditLogEntry, Club, Player, SystemConfig, Tournament, TournamentEntry,
    User, UserIdentity, VenueBooking, VenueStaff,
)
from user_merge import MergeRefused, merge_users, plan_merge, unhandled_user_columns  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


def discord(subject, name=None):
    return ProviderProfile(provider="discord", subject=subject, name=name or subject)


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(SystemConfig(id=1, name="The Old World", slug="tow", legacy_system_name="The Old World", active=True))
    db.add(Club(id=1, name="EGNWGC", slug="egnwgc"))
    db.add(Club(id=2, name="Other", slug="other"))
    old = User(id=26, discord_id="shaun-old", discord_name="shaunwarne93", club_id=1, home_club_id=1,
               player_id=47, display_name="Shaun W",
               created_at=dt.datetime(2026, 6, 21), last_login_at=dt.datetime(2026, 7, 27))
    new = User(id=69, discord_id="shaun-new", discord_name="Shaun", club_id=1, home_club_id=1,
               created_at=dt.datetime(2026, 8, 10), last_login_at=dt.datetime(2026, 9, 10))
    db.add(old)
    db.add(new)
    db.flush()
    attach_identity(db, old, discord("shaun-old", "shaunwarne93"))
    attach_identity(db, new, discord("shaun-new", "Shaun"))
    db.add(Player(id=47, name="Shaun Warne", club_id=1, user_id=26))
    db.add(AdminRole(user_id=26, scope="The Old World", club_id=1))
    db.add(AdminRole(user_id=69, scope="The Old World", club_id=1))   # the same grant twice
    db.add(AdminRole(user_id=26, scope="Kill Team", club_id=1))
    db.add(VenueStaff(user_id=26, club_id=1))
    db.add(AuditLogEntry(actor_user_id=26, actor_name="shaunwarne93", action="role.grant"))
    db.add(VenueBooking(club_id=1, table_id=1, booking_date=dt.date(2026, 9, 20),
                        start_time="18:00", end_time="22:00", contact_name="Shaun", user_id=26))
    db.add(Tournament(id=5, club_id=1, system_id=1, name="Autumn GT", event_date=dt.date(2026, 10, 1)))
    db.add(TournamentEntry(tournament_id=5, display_name="Shaun", user_id=26, player_id=47))
    db.commit()


print("\n1. The merge knows every column that holds a user id")
check("no unhandled user-id columns in the schema", unhandled_user_columns() == [],
      str(unhandled_user_columns()))


print("\n2. The dry-run script shows the merge and writes nothing")
out = subprocess.run(
    [sys.executable, "migrations/merge_users.py", "--keep", "69", "--drop", "26"],
    capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "."},
)
check("the dry run succeeds", out.returncode == 0, out.stdout[-400:] + out.stderr[-400:])
check("and says what it would do", "players #47.user_id -> 69" in out.stdout, out.stdout[-400:])
with Session(database.engine) as db:
    check("the dropped account is still there", db.get(User, 26) is not None)
    check("its player is untouched", db.get(Player, 47).user_id == 26)


print("\n3. Shaun: two Discord accounts become one account with everything")
with Session(database.engine) as db:
    lines = merge_users(db, keep_id=69, drop_id=26)
    db.commit()
with Session(database.engine) as db:
    keep = db.get(User, 69)
    check("the dropped account is gone", db.get(User, 26) is None)
    check("his player and its history now belong to the kept account", db.get(Player, 47).user_id == 69)
    check("the legacy home-club link follows it", keep.player_id == 47, str(keep.player_id))
    roles = db.exec(select(AdminRole).where(AdminRole.user_id == 69)).all()
    check("the duplicate Old World grant collapsed to one",
          sorted(r.scope for r in roles) == ["Kill Team", "The Old World"], str([r.scope for r in roles]))
    check("nothing still points at the dropped account",
          not db.exec(select(AdminRole).where(AdminRole.user_id == 26)).all()
          and not db.exec(select(UserIdentity).where(UserIdentity.user_id == 26)).all())
    check("venue staff moved", db.exec(select(VenueStaff)).first().user_id == 69)
    check("the audit log's actor moved, its name snapshot didn't",
          [(a.actor_user_id, a.actor_name) for a in db.exec(select(AuditLogEntry))] == [(69, "shaunwarne93")])
    check("the booking moved", db.exec(select(VenueBooking)).first().user_id == 69)
    check("the tournament entry moved", db.exec(select(TournamentEntry)).first().user_id == 69)
    check("the name he'd chosen came across", keep.display_name == "Shaun W")
    check("the account is as old as his oldest", keep.created_at == dt.datetime(2026, 6, 21))
    check("and as recent as his latest sign-in", keep.last_login_at == dt.datetime(2026, 9, 10))

    check("his old Discord account still signs in, to the merged account",
          find_user_for_profile(db, discord("shaun-old")).id == 69)
    check("so does the one he uses now", find_user_for_profile(db, discord("shaun-new")).id == 69)
    check("the one he uses now is the one that gets mentioned",
          discord_ids_for_users(db, [69]) == {69: "shaun-new"})
    idents = {i.provider_user_id: i.is_primary for i in db.exec(select(UserIdentity).where(UserIdentity.user_id == 69))}
    check("exactly one primary Discord identity", idents == {"shaun-new": True, "shaun-old": False}, str(idents))
    check("the mirror names the primary", keep.discord_id == "shaun-new")


print("\n4. An account with no Discord takes the dropped account's")
with Session(database.engine) as db:
    g = User(id=80, club_id=1)
    d = User(id=81, discord_id="only-discord", discord_name="Dee", club_id=1)
    db.add(g)
    db.add(d)
    db.flush()
    attach_identity(db, g, ProviderProfile(provider="google", subject="g-80", name="Dee"))
    attach_identity(db, d, discord("only-discord", "Dee"))
    db.commit()
    merge_users(db, keep_id=80, drop_id=81)
    db.commit()
    keep = db.get(User, 80)
    check("its Discord identity stays primary", discord_ids_for_users(db, [80]) == {80: "only-discord"})
    check("and the mirror follows", (keep.discord_id, keep.discord_name) == ("only-discord", "Dee"))


print("\n5. Super-admin authority brings its club")
with Session(database.engine) as db:
    db.add(User(id=90, discord_id="plain", discord_name="Plain", club_id=1))
    db.add(User(id=91, discord_id="boss", discord_name="Boss", club_id=2, is_super_admin=True))
    db.commit()
    merge_users(db, keep_id=90, drop_id=91)
    db.commit()
    keep = db.get(User, 90)
    check("the kept account is super-admin of the club the dropped one ran",
          (keep.is_super_admin, keep.club_id) == (True, 2), str((keep.is_super_admin, keep.club_id)))


def refused(keep_id, drop_id):
    with Session(database.engine) as db:
        before = (len(db.exec(select(User)).all()), len(db.exec(select(UserIdentity)).all()))
        try:
            merge_users(db, keep_id, drop_id)
            db.commit()
            return None, before
        except MergeRefused as e:
            db.rollback()
            return e.problems, before


def counts():
    with Session(database.engine) as db:
        return (len(db.exec(select(User)).all()), len(db.exec(select(UserIdentity)).all()))


print("\n6. Merges that would collide refuse, and write nothing")
with Session(database.engine) as db:
    db.add(User(id=100, discord_id="a", discord_name="A", club_id=1))
    db.add(User(id=101, discord_id="b", discord_name="B", club_id=1))
    db.add(Player(id=200, name="A", club_id=1, user_id=100))
    db.add(Player(id=201, name="B", club_id=1, user_id=101))
    db.add(User(id=102, discord_id="c", discord_name="C", club_id=1))
    db.add(User(id=103, discord_id="d", discord_name="D", club_id=1))
    db.add(TournamentEntry(tournament_id=5, display_name="C", user_id=102))
    db.add(TournamentEntry(tournament_id=5, display_name="D", user_id=103))
    db.add(User(id=104, discord_id="e", discord_name="E", club_id=1, is_super_admin=True))
    db.add(User(id=105, discord_id="f", discord_name="F", club_id=2, is_super_admin=True))
    db.commit()

problems, before = refused(100, 101)
check("two players at the same club", problems and "merge those players first" in problems[0], str(problems))
check("  and nothing was written", counts() == before and Session(database.engine).get(User, 101) is not None)
problems, before = refused(102, 103)
check("both entered in the same tournament", problems and "tournament 5" in problems[0], str(problems))
problems, before = refused(104, 105)
check("super-admins of two different clubs", problems and "two clubs" in problems[0], str(problems))
problems, _ = refused(100, 100)
check("an account into itself", problems and "itself" in problems[0], str(problems))
problems, _ = refused(100, 999)
check("an account that doesn't exist", problems and "does not exist" in problems[0], str(problems))


print("\n7. A user-id column the merge has never heard of makes it refuse")
saved = list(user_merge.USER_REFERENCES)
user_merge.USER_REFERENCES[:] = [r for r in saved if r[0] is not VenueStaff]
with Session(database.engine) as db:
    plan = plan_merge(db, 102, 104)
check("it names the column", any("venue_staff.user_id" in p for p in plan.problems), str(plan.problems))
user_merge.USER_REFERENCES[:] = saved


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
