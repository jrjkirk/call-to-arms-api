"""Bring existing weeks into line with the rule week_pairings.py now enforces:
once a week has pairings, every signup sits in exactly one pairing row.

The code paths that broke it are fixed (2026-09-15). This repairs what they
left behind, and only what a player could still be affected by:

    ghost rows      a pairing pointing at a signup that no longer exists.
                    Both sides gone: deleted, in any week, since nobody can
                    see or play it. One side alive, in a current or upcoming
                    week: the row becomes that player's BYE, or is deleted if
                    they already have another row.
    unplaced        a signup with no row in a current or upcoming week that
                    has pairings: given a BYE, so they are visible and can be
                    re-arranged. Not paired automatically, and nothing is
                    posted: an admin should look at these.
    double-booked   a signup in more than one row in a current or upcoming
                    week: REPORTED ONLY. Which game they keep is a person's
                    decision.

Past weeks are left alone apart from fully-dead ghost rows: rewriting what
happened on a night that is over would change history, experience counts and
the "sat out last session" rule after the fact.

Dry run by default. Safe to run twice.

First dry run on prod, 2026-09-15 (before the fix deployed): nothing to
repair. Every current and upcoming paired week already had each signup in
exactly one row; the only ghosts were AoS 03/09 row 474 and TOW 25/03 row 171,
both past weeks with one live side, both left alone.

    PYTHONPATH=. python migrations/repair_week_pairings.py
    PYTHONPATH=. python migrations/repair_week_pairings.py --apply
"""
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from database import engine
from models import Pairing, PublishState, Signup


def week_date(week: str):
    try:
        return datetime.strptime(week, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def main() -> None:
    apply = "--apply" in sys.argv
    today = datetime.now(ZoneInfo("Europe/London")).date()
    print(f"{'APPLYING' if apply else 'DRY RUN'}; today is {today.isoformat()}\n")

    with Session(engine) as db:
        signups = {s.id: s for s in db.exec(select(Signup)).all()}
        rows = db.exec(select(Pairing).order_by(Pairing.id)).all()
        published = {
            (p.club_id, p.system, p.week)
            for p in db.exec(select(PublishState).where(PublishState.published == True)).all()
        }

        by_week = defaultdict(list)
        for r in rows:
            by_week[(r.club_id, r.system, r.week)].append(r)

        deleted = converted = byes_added = 0

        # Ghost rows.
        for key, week_rows in sorted(by_week.items()):
            d = week_date(key[2])
            upcoming = d is not None and d >= today
            for r in list(week_rows):
                a_live = r.a_signup_id in signups
                b_live = r.b_signup_id is None or r.b_signup_id in signups
                if a_live and b_live:
                    continue
                alive = [sid for sid in (r.a_signup_id, r.b_signup_id) if sid is not None and sid in signups]
                if not alive:
                    print(f"  ghost   {key}: row {r.id} ({r.a_signup_id} v {r.b_signup_id}), nobody left: delete")
                    if apply:
                        db.delete(r)
                    week_rows.remove(r)
                    deleted += 1
                    continue
                if not upcoming:
                    print(f"  ghost   {key}: row {r.id} ({r.a_signup_id} v {r.b_signup_id}), past week: left alone")
                    continue
                sid = alive[0]
                elsewhere = any(
                    x.id != r.id and sid in (x.a_signup_id, x.b_signup_id) for x in week_rows
                )
                name = signups[sid].player_name
                if elsewhere:
                    print(f"  ghost   {key}: row {r.id}, {name} already has another row: delete")
                    if apply:
                        db.delete(r)
                    week_rows.remove(r)
                    deleted += 1
                else:
                    print(f"  ghost   {key}: row {r.id} becomes {name}'s BYE")
                    if apply:
                        r.a_signup_id = sid
                        r.b_signup_id = None
                        r.a_faction = signups[sid].faction
                        r.b_faction = None
                        db.add(r)
                    converted += 1

        # Unplaced and double-booked signups, current and upcoming paired weeks.
        weeks_with_signups = defaultdict(list)
        for s in signups.values():
            weeks_with_signups[(s.club_id, s.system, s.week)].append(s)
        for key, week_signups in sorted(weeks_with_signups.items()):
            d = week_date(key[2])
            if d is None or d < today:
                continue
            week_rows = by_week.get(key, [])
            paired = key in published or any(not r.prearranged for r in week_rows)
            if not paired:
                continue
            refs = defaultdict(int)
            for r in week_rows:
                for sid in (r.a_signup_id, r.b_signup_id):
                    if sid is not None:
                        refs[sid] += 1
            for s in sorted(week_signups, key=lambda s: s.id):
                if refs[s.id] == 0:
                    print(f"  unplaced {key}: {s.player_name} (signup {s.id}): add BYE")
                    if apply:
                        db.add(Pairing(
                            week=s.week, system=s.system,
                            a_signup_id=s.id, b_signup_id=None,
                            status="pending", prearranged=False,
                            a_faction=s.faction, b_faction=None,
                            club_id=s.club_id,
                        ))
                    byes_added += 1
                elif refs[s.id] > 1:
                    print(f"  DOUBLE  {key}: {s.player_name} (signup {s.id}) is in {refs[s.id]} rows: needs an admin")

        if apply:
            db.commit()
        print(f"\nghost rows deleted {deleted}, converted to BYE {converted}, BYEs added {byes_added}"
              + ("" if apply else "  (dry run, nothing written)"))


if __name__ == "__main__":
    main()
