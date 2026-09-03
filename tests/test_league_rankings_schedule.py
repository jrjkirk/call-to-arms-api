"""The league standings post, on a schedule the club controls.

It used to be `cron: '0 19 * * 4'` and nothing else — no settings, no dedup, no
catch-up. A weekly cron has one slot, and GitHub honoured none of them: against
a 20:00 BST target the real runs landed at 21:00, 21:09, 21:12, 21:17, 21:44,
21:54, 22:22, and twice past midnight into Friday at 01:39 and 03:24. Miss the
slot and the week's post was simply gone, because nothing recorded one was due.

These assert the replacement: a per-club-system day and time, a dedup that
survives a late or repeated run, and the render handed to a GitHub runner from
anywhere that can't draw the image itself.

Run: PYTHONPATH=. python tests/test_league_rankings_schedule.py
"""
import os
import pathlib
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

# Throwaway SQLite, set before database.py builds its engine. Never point this
# at a real DATABASE_URL: the job posts to Discord.
_DB = pathlib.Path(tempfile.mkdtemp()) / "league_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["GH_DISPATCH_TOKEN"] = "fake-token-for-test"

from sqlmodel import Session, SQLModel  # noqa: E402

import database  # noqa: E402
from models import Club, ClubSetting, ClubSystem, ClubWebhook, SystemConfig  # noqa: E402
from week_logic import _is_league_rankings_due  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


UK = ZoneInfo("Europe/London")
SYSTEM = "The Old World"
SLUG = "TheOldWorld"


print("\n1. The due-check itself")
base = {"day": "Thursday", "time": "20:00", "last_posted": None}
thu = datetime(2026, 9, 3, 20, 30, tzinfo=UK)          # Thursday, after the time
check("fires on the configured day, after the time",
      _is_league_rankings_due(base, thu, "2026-09-03"))
check("does not fire before the time",
      not _is_league_rankings_due(base, datetime(2026, 9, 3, 19, 59, tzinfo=UK), "2026-09-03"))
check("does not fire on the wrong day",
      not _is_league_rankings_due(base, datetime(2026, 9, 2, 22, 0, tzinfo=UK), "2026-09-02"))
# The whole point of the rewrite: a run hours late still posts, same day.
check("a run hours late still fires",
      _is_league_rankings_due(base, datetime(2026, 9, 3, 23, 58, tzinfo=UK), "2026-09-03"))
# ...but never spills into the next day, which is where the old cron's 01:39
# and 03:24 runs put the post.
check("but not once the day has rolled over",
      not _is_league_rankings_due(base, datetime(2026, 9, 4, 3, 24, tzinfo=UK), "2026-09-04"))
check("already posted today means done",
      not _is_league_rankings_due({**base, "last_posted": "2026-09-03"}, thu, "2026-09-03"))
check("yesterday's post doesn't block today",
      _is_league_rankings_due({**base, "last_posted": "2026-08-27"}, thu, "2026-09-03"))
check("an unrecognised day never fires, rather than defaulting to one",
      not _is_league_rankings_due({**base, "day": "Someday"}, thu, "2026-09-03"))


SQLModel.metadata.create_all(database.engine)
with Session(database.engine) as db:
    db.add(Club(id=1, name="EG NWGC", slug="egnwgc"))
    db.add(SystemConfig(id=1, name=SYSTEM, slug="tow", legacy_system_name=SYSTEM, active=True))
    db.add(ClubSystem(club_id=1, system_id=1, enabled=True, league_enabled=True,
                      session_day="Wednesday", session_cadence="weekly"))
    db.add(ClubWebhook(club_id=1, webhook_type="league_rankings", system_id=1,
                       url="https://discord.test/webhook"))
    db.commit()

import scripts.run_league_rankings_check as job  # noqa: E402


class _Frozen(datetime):
    """The job reads the clock itself, so pin it rather than waiting for Thursday."""
    @classmethod
    def now(cls, tz=None):
        return thu


job.datetime = _Frozen

DISPATCHES = []
job.dispatch_workflow = lambda wf, inputs=None: (DISPATCHES.append(wf), (True, ""))[1]

SENT = []


def _fake_sender():
    def collect(db, club, system_config):
        return (club.slug, system_config.name, "https://webhook.test", ["row"])

    def send(j):
        SENT.append(j[0])
        return True

    return collect, None, send


def setting(name):
    with Session(database.engine) as db:
        return database.get_setting(db, 1, f"league_rankings_{SLUG}_{name}")


print("\n2. Defaults reproduce the old cron, so nobody's post moves")
with Session(database.engine) as db:
    s = job.settings_for(db, 1, SYSTEM)
check("day defaults to Thursday", s["day"] == "Thursday", s["day"])
check("time defaults to 20:00", s["time"] == "20:00", s["time"])


print("\n3. Where the image can't be drawn, a runner is asked — once")
job._load_sender = lambda: None
job.main()
check("one dispatch was queued", len(DISPATCHES) == 1, str(DISPATCHES))
check("it queued the league workflow",
      DISPATCHES == ["league-rankings-check.yml"], str(DISPATCHES))
check("the request is recorded", setting("post_requested") == "2026-09-03")
check("nothing is marked posted — nothing was", setting("last_posted") is None)
job.main()
job.main()
check("further ticks don't queue more runs", len(DISPATCHES) == 1, str(DISPATCHES))


print("\n4. The runner posts, and records that it did")
job._load_sender = _fake_sender
job.main()
check("posted exactly once", SENT == ["egnwgc"], str(SENT))
check("last_posted recorded", setting("last_posted") == "2026-09-03")


print("\n5. The backstop must not post the standings twice")
job.main()
check("no second post", SENT == ["egnwgc"], str(SENT))


print("\n6. Next week it goes again")
next_thu = datetime(2026, 9, 10, 20, 5, tzinfo=UK)


class _NextWeek(datetime):
    @classmethod
    def now(cls, tz=None):
        return next_thu


job.datetime = _NextWeek
job.main()
check("posts again the following week", SENT == ["egnwgc", "egnwgc"], str(SENT))
check("last_posted moved on", setting("last_posted") == "2026-09-10")


print("\n7. A club that switched league posts off is left alone")
with Session(database.engine) as db:
    database.upsert_setting(db, 1, database.posting_key("league", SYSTEM), "false")
    row = db.get(ClubSetting, (1, f"league_rankings_{SLUG}_last_posted"))
    if row:
        db.delete(row)
    db.commit()
before = len(SENT)
job.main()
check("nothing posted with the League switch off", len(SENT) == before, str(SENT))

print("\n8. A club with no rankings channel doesn't summon a runner either")
with Session(database.engine) as db:
    database.upsert_setting(db, 1, database.posting_key("league", SYSTEM), "true")
    hook = db.exec(__import__("sqlmodel").select(ClubWebhook)).first()
    db.delete(hook)
    row = db.get(ClubSetting, (1, f"league_rankings_{SLUG}_last_posted"))
    if row:
        db.delete(row)
    db.commit()
dispatches_before = len(DISPATCHES)
job._load_sender = lambda: None
job.main()
check("no workflow queued when there is nowhere to post",
      len(DISPATCHES) == dispatches_before, str(DISPATCHES))

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
