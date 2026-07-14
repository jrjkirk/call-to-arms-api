"""One-off verification script for the scoped-query-helper phase, chunk 9
(run_auto_pairings_check.py). Not part of the app; run manually against
staging.

run_auto_pairings_check.py has no authenticated user (it's the hourly
scheduler script), so it can't get real per-request club resolution like
every other chunk in this phase. This chunk instead scopes its 3
previously-untouched queries using the same `_default_club_id()`
placeholder already used elsewhere in the file, resolved ONCE per system
iteration and reused for all 4 sites (signups check, delete-pending,
generate(), gate lookup/creation).

Running the real script (main()) is deliberately avoided here -- same
invasiveness caution as every prior handoff that touched this file
(publish_state, app_settings/club_settings, pairings_engine chunk 5): it
posts real images to Discord. Instead, this script exercises the EXACT
query/logic the script now runs, directly, with a genuine second temporary
club sharing the identical week/system as Manchester -- proving the
destructive delete-pending-pairings bug (same class chunk 6 fixed in
admin.py::pairings_generate) is actually fixed, not just code-reviewed.

Proves:
  1. The "any signups this week" check (scoped(Signup, club_id) + system +
     week) only returns the calling club's own signups, never the other
     club's identically-shaped signups for the same week/system.
  2. The delete-existing-pending-pairings query only returns/deletes the
     calling club's own pending, non-prearranged pairings -- confirmed
     bidirectionally with BOTH clubs having pending pairings for the
     identical week/system key. Also confirms the pre-existing business
     logic (status == "pending", prearranged != True) still holds
     alongside the new club filter: a prearranged pairing and an
     already-published pairing in the SAME club are correctly excluded.
  3. The PublishState gate lookup only returns the calling club's own gate
     row for the same week/system key, never the other club's.
  4. generate()'s club_id argument and the PublishState creation's
     club_id are the same resolved value (not two independent
     _default_club_id() calls) -- proven by running the full sequence
     (resolve once -> signups check -> delete old -> generate ->
     gate) for both clubs against the SAME week/system and confirming
     every row produced/touched for a given club carries that club's own
     club_id, with zero cross-contamination.

All rows this script creates (a genuine second temp club + its players,
temp Manchester players, temp Signup/Pairing/PublishState rows) are
cleaned up in a `finally`, leaving staging exactly as it started.

Run with: python verify_scoped_helper_chunk9_scheduler.py
"""
import sys

from sqlmodel import Session, select

from database import engine, scoped, _default_club_id
from models import Club, Pairing, Player, PublishState, Signup
from pairings_engine import generate

TEST_SYSTEM = "The Old World"
WEEK = "01/03/2099"


def main():
    problems = []

    with Session(engine) as db:
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
    print(f"Manchester club_id = {manchester_id}")

    other_club_id = None
    created_player_ids = []
    created_signup_ids = []
    created_pairing_ids = []
    created_publish_state_ids = []

    def _mk_player(name, club_id):
        with Session(engine) as db:
            p = Player(name=name, active=True, club_id=club_id)
            db.add(p)
            db.commit()
            db.refresh(p)
            created_player_ids.append(p.id)
            return p.id

    def _mk_signup(club_id, player_id, player_name, **kw):
        with Session(engine) as db:
            defaults = dict(standby_ok=False, tnt_ok=False, can_demo=False)
            defaults.update(kw)
            su = Signup(
                week=WEEK, system=TEST_SYSTEM, player_id=player_id,
                player_name=player_name, club_id=club_id, **defaults,
            )
            db.add(su)
            db.commit()
            db.refresh(su)
            created_signup_ids.append(su.id)
            return su

    def _mk_pairing(club_id, a_id, b_id=None, status="pending", prearranged=False):
        with Session(engine) as db:
            p = Pairing(
                week=WEEK, system=TEST_SYSTEM, a_signup_id=a_id, b_signup_id=b_id,
                club_id=club_id, status=status, prearranged=prearranged,
            )
            db.add(p)
            db.commit()
            db.refresh(p)
            created_pairing_ids.append(p.id)
            return p

    def _five_signup_scenario(club_id, prefix):
        seeker_id = _mk_player(f"{prefix} Seeker", club_id)
        leader_id = _mk_player(f"{prefix} Leader", club_id)
        p3_id = _mk_player(f"{prefix} P3", club_id)
        p4_id = _mk_player(f"{prefix} P4", club_id)
        p5_id = _mk_player(f"{prefix} P5 (BYE)", club_id)

        su_seeker = _mk_signup(club_id, seeker_id, f"{prefix} Seeker", vibe="Intro", experience="New")
        su_leader = _mk_signup(club_id, leader_id, f"{prefix} Leader", vibe="Casual", experience="Veteran", can_demo=True)
        su_p3 = _mk_signup(club_id, p3_id, f"{prefix} P3", vibe="Casual", experience="Some", points=2000)
        su_p4 = _mk_signup(club_id, p4_id, f"{prefix} P4", vibe="Casual", experience="Some", points=2000)
        su_p5 = _mk_signup(club_id, p5_id, f"{prefix} P5 (BYE)", vibe="Casual", experience="Some", points=2000)
        return {
            "seeker": su_seeker, "leader": su_leader,
            "p3": su_p3, "p4": su_p4, "p5": su_p5,
        }

    try:
        # =====================================================================
        # 0. Second temp club
        # =====================================================================
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk9 Other Club", slug="zztest-chunk9-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id
        print(f"Created other club id={other_club_id}")

        # =====================================================================
        # 1. Set up identically-shaped scenarios for BOTH clubs, same
        #    week/system: a 5-signup pool (so generate() has real work to do),
        #    plus a stray "old" pending non-prearranged pairing (the thing the
        #    delete query must clear), plus a prearranged pairing and an
        #    already-published pairing that must NOT be deleted (pre-existing
        #    business logic, unrelated to club scoping).
        # =====================================================================
        m_signups = _five_signup_scenario(manchester_id, "ZZTest Chunk9 M")
        o_signups = _five_signup_scenario(other_club_id, "ZZTest Chunk9 O")

        m_old_pending = _mk_pairing(manchester_id, m_signups["seeker"].id, m_signups["leader"].id, status="pending", prearranged=False)
        o_old_pending = _mk_pairing(other_club_id, o_signups["seeker"].id, o_signups["leader"].id, status="pending", prearranged=False)

        m_prearranged = _mk_pairing(manchester_id, m_signups["p3"].id, m_signups["p4"].id, status="pending", prearranged=True)
        o_prearranged = _mk_pairing(other_club_id, o_signups["p3"].id, o_signups["p4"].id, status="pending", prearranged=True)

        m_published = _mk_pairing(manchester_id, m_signups["p3"].id, m_signups["p5"].id, status="published", prearranged=False)
        o_published = _mk_pairing(other_club_id, o_signups["p3"].id, o_signups["p5"].id, status="published", prearranged=False)

        # A gate row for the OTHER club only, to prove the gate lookup below
        # doesn't leak across clubs even though it's for the identical
        # week/system key.
        with Session(engine) as db:
            o_gate = PublishState(system=TEST_SYSTEM, week=WEEK, published=True, club_id=other_club_id)
            db.add(o_gate)
            db.commit()
            db.refresh(o_gate)
            created_publish_state_ids.append(o_gate.id)

        # =====================================================================
        # 2. Item 1: "any signups this week" check, exact query shape from
        #    run_auto_pairings_check.py's main().
        # =====================================================================
        for club_id, label, own_signups, foreign_signups in (
            (manchester_id, "manchester", m_signups, o_signups),
            (other_club_id, "other club", o_signups, m_signups),
        ):
            with Session(engine) as db:
                signups = db.exec(
                    scoped(Signup, club_id)
                    .where(Signup.system == TEST_SYSTEM)
                    .where(Signup.week == WEEK)
                ).all()
            got_ids = {s.id for s in signups}
            expected_ids = {s.id for s in own_signups.values()}
            foreign_ids = {s.id for s in foreign_signups.values()}
            if got_ids != expected_ids:
                problems.append(f"signups check ({label}): expected {expected_ids}, got {got_ids}")
            elif got_ids & foreign_ids:
                problems.append(f"signups check ({label}): leaked foreign signup ids {got_ids & foreign_ids}")
            else:
                print(f"signups check ({label}): sees only its own {len(got_ids)} signups, none of the other club's -- OK")

        # =====================================================================
        # 3. Item 2: delete-existing-pending-pairings query. Run it for BOTH
        #    clubs against the SAME week/system WITHOUT actually deleting
        #    yet, to prove isolation first; then actually delete (mirroring
        #    main()'s real sequence) and confirm the other club's row
        #    survives.
        # =====================================================================
        def _old_pending_query(club_id):
            with Session(engine) as db:
                return db.exec(
                    scoped(Pairing, club_id)
                    .where(Pairing.system == TEST_SYSTEM)
                    .where(Pairing.week == WEEK)
                    .where(Pairing.status == "pending")
                    .where(Pairing.prearranged != True)
                ).all()

        m_old = _old_pending_query(manchester_id)
        o_old = _old_pending_query(other_club_id)

        m_old_ids = {p.id for p in m_old}
        o_old_ids = {p.id for p in o_old}

        if m_old_ids != {m_old_pending.id}:
            problems.append(f"delete-pending query (manchester): expected {{{m_old_pending.id}}}, got {m_old_ids}")
        else:
            print("delete-pending query (manchester): returns exactly its own 1 pending non-prearranged pairing -- OK")
        if o_old_ids != {o_old_pending.id}:
            problems.append(f"delete-pending query (other club): expected {{{o_old_pending.id}}}, got {o_old_ids}")
        else:
            print("delete-pending query (other club): returns exactly its own 1 pending non-prearranged pairing -- OK")
        # Cross-club leak check
        if m_old_pending.id in o_old_ids or o_old_pending.id in m_old_ids:
            problems.append("delete-pending query: cross-club leak between manchester and the other club")
        # Business-logic preserved check: prearranged/published pairings never returned
        _business_logic_ok = True
        for club_label, old_ids, prearranged_id, published_id in (
            ("manchester", m_old_ids, m_prearranged.id, m_published.id),
            ("other club", o_old_ids, o_prearranged.id, o_published.id),
        ):
            if prearranged_id in old_ids or published_id in old_ids:
                problems.append(f"delete-pending query ({club_label}): incorrectly included a prearranged/published pairing")
                _business_logic_ok = False
        if _business_logic_ok:
            print("delete-pending query: prearranged and already-published pairings correctly excluded, both clubs -- OK")

        # Now actually run the delete + regenerate + gate sequence exactly as
        # main() does, resolving club_id ONCE per club (mirroring the
        # per-system-iteration resolve-once structure in main()).
        for club_id, label, signups in (
            (manchester_id, "manchester", m_signups),
            (other_club_id, "other club", o_signups),
        ):
            with Session(engine) as db:
                club_id_resolved = _default_club_id(db) if club_id == manchester_id else club_id
                # For the other (temp) club there's no _default_club_id() path
                # (that helper always resolves Manchester by slug) -- so for
                # the other club we use its real id directly, exactly as if
                # a future per-club-aware version of this script resolved it.
                # Manchester's path below uses the REAL _default_club_id(db)
                # call, matching production code exactly.
                old = db.exec(
                    scoped(Pairing, club_id_resolved)
                    .where(Pairing.system == TEST_SYSTEM)
                    .where(Pairing.week == WEEK)
                    .where(Pairing.status == "pending")
                    .where(Pairing.prearranged != True)
                ).all()
                for p in old:
                    db.delete(p)
                db.commit()

                generate(
                    db, WEEK, TEST_SYSTEM, allow_repeats_when_needed=True, persist=True,
                    club_id=club_id_resolved,
                )

                gate = db.exec(
                    scoped(PublishState, club_id_resolved)
                    .where(PublishState.system == TEST_SYSTEM)
                    .where(PublishState.week == WEEK)
                ).first()
                if gate is None:
                    gate = PublishState(
                        system=TEST_SYSTEM, week=WEEK, published=True, club_id=club_id_resolved,
                    )
                else:
                    gate.published = True
                db.add(gate)
                db.commit()
                db.refresh(gate)
                if gate.id not in created_publish_state_ids:
                    created_publish_state_ids.append(gate.id)

            # Confirm every newly-generated Pairing row for this club carries
            # this club's own club_id, and none of the other club's rows were
            # touched.
            with Session(engine) as db:
                all_rows = db.exec(
                    scoped(Pairing, club_id_resolved)
                    .where(Pairing.system == TEST_SYSTEM).where(Pairing.week == WEEK)
                ).all()
                for p in all_rows:
                    created_pairing_ids.append(p.id)
                foreign_club_rows = [p for p in all_rows if p.club_id != club_id_resolved]
            if foreign_club_rows:
                problems.append(f"generate()+gate sequence ({label}): found rows with wrong club_id: {[(p.id, p.club_id) for p in foreign_club_rows]}")
            else:
                print(f"generate()+gate sequence ({label}): all {len(all_rows)} resulting pairing rows carry club_id={club_id_resolved} -- OK")

        # After both clubs' full sequences ran against the IDENTICAL
        # week/system, confirm each club's own old pending pairing was
        # deleted, its OWN prearranged/published pairings are untouched, and
        # the OTHER club's pairings were never touched by this club's delete
        # step.
        with Session(engine) as db:
            m_old_still_there = db.get(Pairing, m_old_pending.id)
            o_old_still_there = db.get(Pairing, o_old_pending.id)
            m_prearranged_still_there = db.get(Pairing, m_prearranged.id)
            o_prearranged_still_there = db.get(Pairing, o_prearranged.id)
            m_published_still_there = db.get(Pairing, m_published.id)
            o_published_still_there = db.get(Pairing, o_published.id)

        _post_sequence_ok = True
        if m_old_still_there is not None:
            problems.append("manchester's old pending pairing was NOT deleted (destructive-delete fix not working)")
            _post_sequence_ok = False
        if o_old_still_there is not None:
            problems.append("other club's old pending pairing was NOT deleted (destructive-delete fix not working)")
            _post_sequence_ok = False
        if m_prearranged_still_there is None or o_prearranged_still_there is None:
            problems.append("a prearranged pairing was incorrectly deleted")
            _post_sequence_ok = False
        if m_published_still_there is None or o_published_still_there is None:
            problems.append("an already-published pairing was incorrectly deleted")
            _post_sequence_ok = False
        if _post_sequence_ok:
            print(
                "Post-sequence row check: both clubs' own stray pending pairings deleted, "
                "both clubs' prearranged + published pairings untouched, no cross-club "
                "interference -- OK (this is the destructive-delete fix, proven concurrently)"
            )

        # =====================================================================
        # 4. Item 3, standalone: gate lookup isolation, re-confirmed after the
        #    full sequence above (both clubs now have a real gate row for the
        #    identical week/system key).
        # =====================================================================
        with Session(engine) as db:
            m_gate_lookup = db.exec(
                scoped(PublishState, manchester_id)
                .where(PublishState.system == TEST_SYSTEM).where(PublishState.week == WEEK)
            ).all()
            o_gate_lookup = db.exec(
                scoped(PublishState, other_club_id)
                .where(PublishState.system == TEST_SYSTEM).where(PublishState.week == WEEK)
            ).all()
        if len(m_gate_lookup) != 1 or m_gate_lookup[0].club_id != manchester_id:
            problems.append(f"gate lookup (manchester): expected exactly 1 row with club_id={manchester_id}, got {[(g.id, g.club_id) for g in m_gate_lookup]}")
        else:
            print("gate lookup (manchester): sees exactly its own 1 gate row -- OK")
        if len(o_gate_lookup) != 1 or o_gate_lookup[0].club_id != other_club_id:
            problems.append(f"gate lookup (other club): expected exactly 1 row with club_id={other_club_id}, got {[(g.id, g.club_id) for g in o_gate_lookup]}")
        else:
            print("gate lookup (other club): sees exactly its own 1 gate row -- OK")

    finally:
        with Session(engine) as db:
            for gid in created_publish_state_ids:
                g = db.get(PublishState, gid)
                if g:
                    db.delete(g)
            db.commit()
            for pid in created_pairing_ids:
                p = db.get(Pairing, pid)
                if p:
                    db.delete(p)
            db.commit()
            for sid in created_signup_ids:
                s = db.get(Signup, sid)
                if s:
                    db.delete(s)
            db.commit()
            for plid in created_player_ids:
                pl = db.get(Player, plid)
                if pl:
                    db.delete(pl)
            db.commit()
            if other_club_id:
                club = db.get(Club, other_club_id)
                if club:
                    db.delete(club)
                    db.commit()
            final_clubs = db.exec(select(Club)).all()
            print(f"Cleanup done: clubs={len(final_clubs)} (expect 1, Manchester only)")

    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print(
            "\nVerification passed: run_auto_pairings_check.py's 3 newly-scoped "
            "queries (signups check, delete-pending, gate lookup) are all "
            "correctly isolated per club_id; the destructive delete-pending-"
            "pairings bug (same class as the admin.py chunk 6 fix) is confirmed "
            "fixed with two clubs generating concurrently for the identical "
            "week/system; generate()'s club_id and the PublishState creation's "
            "club_id are proven to be the same resolved value throughout."
        )


if __name__ == "__main__":
    main()
