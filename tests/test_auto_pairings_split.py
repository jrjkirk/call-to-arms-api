"""Auto-pairings pair/post split, written against the failure that caused it.

On 01/09/2026 The Old World at EGNWGC was configured correctly — on, Tuesday,
21:00 — and still lost its week. The job lived only on a GitHub cron that fires
about five times a day; that day's last run arrived at 20:57, three minutes
early, and nothing came back before midnight. The same log showed Age of Sigmar
and Kill Team being quietly written off every week by a "no signups" branch that
latched hours before anyone had signed up.

So the two halves are now independent — pairing runs on the reliable in-process
tick, rendering stays on a runner — and each block below asserts one of the
things that went wrong stays fixed.

Run: PYTHONPATH=. python tests/test_auto_pairings_split.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

# A throwaway SQLite file, set before database.py is imported and builds its
# engine. Never point this at a real DATABASE_URL: the job posts to Discord.
_DB = pathlib.Path(tempfile.mkdtemp()) / "auto_pairings_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["GH_DISPATCH_TOKEN"] = "fake-token-for-test"

from sqlmodel import Session, SQLModel, select  # noqa: E402

import database  # noqa: E402
from models import (  # noqa: E402
    Club, ClubSetting, ClubSystem, Pairing, Player, PublishState, Signup, SystemConfig,
)

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


SYSTEM = "The Old World"
SLUG = "TheOldWorld"
TARGET = "02/09/2026"                                        # the Wednesday session
NOW = datetime(2026, 9, 1, 21, 30, tzinfo=ZoneInfo("Europe/London"))   # Tuesday, after 21:00

SQLModel.metadata.create_all(database.engine)

with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc"))
    db.add(SystemConfig(id=1, name=SYSTEM, slug="tow", legacy_system_name=SYSTEM,
                        uses_points=True, active=True))
    db.add(ClubSystem(club_id=1, system_id=1, enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    # Exactly EGNWGC's real settings on the night it failed.
    for key, value in [("enabled", "true"), ("day", "Tuesday"), ("time", "21:00"),
                       ("last_week", "26/08/2026")]:
        db.add(ClubSetting(club_id=1, key=f"auto_pairings_{SLUG}_{key}", value=value))
    for i, name in enumerate(["Aslan", "Ed", "Ben", "Jonathan", "Damian", "Alex"], start=1):
        db.add(Player(id=i, name=name))
        db.add(Signup(week=TARGET, system=SYSTEM, player_id=i, player_name=name,
                      faction="Empire of Man", points=2000, eta="18:00", club_id=1))
    db.commit()

import scripts.run_auto_pairings_check as job  # noqa: E402


class _Frozen(datetime):
    """The job reads the clock itself, so pin it rather than waiting for 21:00."""
    @classmethod
    def now(cls, tz=None):
        return NOW


job.datetime = _Frozen

DISPATCHES = []
job.dispatch_workflow = lambda wf, inputs=None: (
    DISPATCHES.append((wf, inputs)), (True, "")
)[1]

POSTS = []


def _fake_poster(db, system, week, club_id):
    POSTS.append((system, week, club_id))
    return True


def setting(name):
    with Session(database.engine) as db:
        return database.get_setting(db, 1, f"auto_pairings_{SLUG}_{name}")


def pairing_ids():
    with Session(database.engine) as db:
        return {p.id for p in db.exec(select(Pairing).where(Pairing.club_id == 1)).all()}


def published():
    with Session(database.engine) as db:
        gate = db.exec(select(PublishState).where(PublishState.club_id == 1)).first()
        return gate.published if gate else None


print("\n1. The API container has no renderer: it must still pair, then delegate")
job._load_image_poster = lambda: None       # what a matplotlib-less container sees
job.main()
check("pairings were generated", len(pairing_ids()) == 3)
check("the week was published", published() is True)
check("last_week advanced to the target week", setting("last_week") == TARGET)
check("the image was handed to a GitHub runner", len(DISPATCHES) == 1)
check("it dispatched the auto-pairings workflow",
      bool(DISPATCHES) and DISPATCHES[0][0] == "auto-pairings-check.yml")
check("posted_week stays unset — nothing was rendered here",
      setting("posted_week") is None)
check("post_requested_week records the ask", setting("post_requested_week") == TARGET)

print("\n2. Later ticks must not re-roll pairings or spam dispatches")
# The in-process tick is every five minutes. The old job deleted and
# regenerated every time it ran, so a second visit after a hand-published week
# would silently hand players a different opponent.
before = pairing_ids()
job.main()
job.main()
check("pairings are not deleted and re-rolled", pairing_ids() == before)
check("no dispatch storm from repeated ticks", len(DISPATCHES) == 1)

print("\n3. The runner picks it up, posts, and records that it did")
job._load_image_poster = lambda: _fake_poster
job.main()
check("the image posted exactly once", len(POSTS) == 1)
check("it posted the right club/system/week", POSTS == [(SYSTEM, TARGET, 1)])
check("posted_week is now set", setting("posted_week") == TARGET)

print("\n4. The cron backstop must not double-post a week already done")
job.main()
check("no second post", len(POSTS) == 1)

print("\n5. 'No signups yet' must not write the week off")
# The bug that was killing Age of Sigmar and Kill Team every week: the first
# due tick can land days before a session, and latching the dedup there meant
# anyone signing up afterwards could never be paired.
with Session(database.engine) as db:
    for row in db.exec(select(Signup)).all():
        db.delete(row)
    for row in db.exec(select(Pairing)).all():
        db.delete(row)
    for key in ("last_week", "posted_week", "post_requested_week"):
        row = db.get(ClubSetting, (1, f"auto_pairings_{SLUG}_{key}"))
        if row:
            db.delete(row)
    db.commit()

job._load_image_poster = lambda: None
job.main()
check("last_week is NOT latched when nobody has signed up yet",
      setting("last_week") is None)

with Session(database.engine) as db:
    for i, name in [(1, "Aslan"), (2, "Ed")]:
        db.add(Signup(week=TARGET, system=SYSTEM, player_id=i, player_name=name,
                      faction="Empire of Man", points=2000, eta="18:00", club_id=1))
    db.commit()

job.main()
check("players who sign up late still get paired on a later tick",
      setting("last_week") == TARGET)

print("\n6. A club with pairings posts switched off never queues a runner")
with Session(database.engine) as db:
    database.upsert_setting(db, 1, database.posting_key("pairings", SYSTEM), "false")
    row = db.get(ClubSetting, (1, f"auto_pairings_{SLUG}_post_requested_week"))
    if row:
        db.delete(row)
    db.commit()
dispatches_before = len(DISPATCHES)
job.main()
check("no dispatch when pairings posts are switched off",
      len(DISPATCHES) == dispatches_before)

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
