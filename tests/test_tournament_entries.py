"""Who owns an entry, and who may report a game.

Regression test for a real bug: add_entry defaulted player_id to the CALLER's
own player when none was given, so every walk-in a TO added at the door came
out owned by the TO. The TO then matched as "entered" and could self-report
those games as their own.

Run: PYTHONPATH=. python tests/test_tournament_entries.py
"""
import os
import sys
import tempfile

DB = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["DATABASE_URL"] = "sqlite:///" + DB

from sqlmodel import SQLModel, Session, select   # noqa: E402
import database                                   # noqa: E402
from database import engine                       # noqa: E402
import models as M                                # noqa: E402
import tournaments as T                           # noqa: E402
from fastapi import HTTPException                 # noqa: E402

database.WRITE_ALLOWED_TABLES = database.WRITE_ALLOWED_TABLES | {
    "clubs", "users", "players", "systems", "club_systems"}
SQLModel.metadata.create_all(engine)

FAILURES = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond: FAILURES.append(label)


class Body:
    """Stands in for EntryBody — only the fields add_entry reads."""
    def __init__(self, **kw):
        for f in ("display_name", "contact_email", "faction", "army_list", "notes", "player_id"):
            setattr(self, f, kw.get(f))


with Session(engine) as db:
    club = M.Club(name="C", slug="egnwgc", active=True); db.add(club); db.commit(); db.refresh(club)
    sc = M.SystemConfig(name="HH", slug="hh", legacy_system_name="HH", active=True)
    db.add(sc); db.commit(); db.refresh(sc)
    db.add(M.ClubSystem(club_id=club.id, system_id=sc.id, enabled=True,
                        session_day="Friday", session_cadence="weekly"))
    to = M.User(discord_id="1", discord_name="TO", club_id=club.id,
                home_club_id=club.id, is_super_admin=True, is_platform_admin=True)
    pl = M.User(discord_id="2", discord_name="Sam", club_id=club.id,
                home_club_id=club.id, is_platform_admin=True)
    db.add(to); db.add(pl); db.commit(); db.refresh(to); db.refresh(pl)
    db.add(M.Player(club_id=club.id, name="Joel", user_id=to.id, active=True))
    db.add(M.Player(club_id=club.id, name="Sam", user_id=pl.id, active=True))
    t = M.Tournament(club_id=club.id, system_id=sc.id, name="Ep6",
                     event_date=__import__("datetime").date(2026, 11, 14),
                     rounds=3, status="open")
    db.add(t); db.commit(); db.refresh(t)

    print("\nEntry ownership")
    T.add_entry(t.id, Body(faction="Sons of Horus"), user=pl, club_id=club.id, db=db)
    for n in ("Ada", "Ben"):
        T.add_entry(t.id, Body(display_name=n), user=to, club_id=club.id, db=db)

    entries = {e.display_name: e for e in db.exec(select(M.TournamentEntry)).all()}
    check("a player registering themselves owns their entry",
          entries["Sam"].user_id == pl.id and entries["Sam"].player_id is not None)
    check("a walk-in the TO adds is owned by nobody",
          entries["Ada"].user_id is None and entries["Ada"].player_id is None,
          f"user_id={entries['Ada'].user_id} player_id={entries['Ada'].player_id}")
    check("the TO's own player id is not stamped on a walk-in",
          entries["Ben"].player_id is None)

    print("\nWho may report")
    try:
        T._my_entry(db, t, to, club.id)
        check("a TO who isn't entered has no entry", False, "found one")
    except HTTPException as e:
        check("a TO who isn't entered has no entry", e.status_code == 403)
    check("an entered player resolves to their own entry",
          T._my_entry(db, t, pl, club.id).display_name == "Sam")

    print("\nDouble entry")
    try:
        T.add_entry(t.id, Body(), user=pl, club_id=club.id, db=db)
        check("a player can't enter twice", False, "allowed")
    except HTTPException as e:
        check("a player can't enter twice", e.status_code == 409)

    print("\nStandings visibility")
    t.scoring = {"sports_enabled": True, "painting_enabled": True}
    db.add(t); db.commit()
    admin_view = T._standings(db, t, for_admin=True)
    player_view = T._standings(db, t, for_admin=False)
    check("a TO sees sportsmanship and painting",
          "sports" in admin_view[0] and "painting" in admin_view[0])
    check("a player sees neither",
          "sports" not in player_view[0] and "painting" not in player_view[0])
    check("both see the same players in the same order",
          [r["name"] for r in admin_view] == [r["name"] for r in player_view])

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
