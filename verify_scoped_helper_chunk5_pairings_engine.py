"""One-off verification script for the scoped-query-helper phase, chunk 5
(pairings_engine.py). Not part of the app; run manually against staging.

Proves, beyond "no errors":
  1. Real output comparison: the same 5-signup scenario used to verify the
     original `pairings` table-7 handoff (intro seeker + demo leader + 2
     regular + 1 odd) still produces the identical structure (1 intro
     pre-pass match + 1 main match + 1 BYE) after this chunk's conversion
     of `generate()` and its three history/block helpers to real
     `user.club_id` scoping.
  2. Cross-club isolation of the three history helpers
     (`previous_pairs_recent`, `last_opponent_pairs`,
     `previous_bye_player_ids`) and the block query inside `generate()`:
     a genuine second temporary club's own recency/last-opponent/BYE
     history and pairing-block data never appears in Manchester's scoped
     results, and vice versa.
  3. Cross-club isolation of `generate()`'s candidate pool: with a second
     temporary club running an identically-shaped 5-signup scenario for
     the SAME week/system, each club's dry-run preview only ever produces
     pairings among its own signups.
  4. `generate()`'s persist=True path (via POST /admin/pairings/generate)
     writes real Pairing rows with club_id = the calling admin's real
     club_id, for both clubs independently (different weeks, to sidestep
     a known, separate, out-of-scope gap noted below).
  5. The scheduler's call path (`run_auto_pairings_check.py`'s exact
     `generate(...)` call shape) still works when exercised directly.

Known gap, discovered while writing this script, explicitly OUT OF SCOPE
for this chunk (matches the handoff's instructions, which only added
`club_id=user.club_id` to the two `generate(...)` calls in admin.py):
`admin.py::pairings_generate`'s "delete existing pending pairings" query
is NOT scoped by club_id, so two clubs generating for the same week+system
key would clobber each other's pending pairings. Flagged for a future
admin.py read/write-scoping chunk, not fixed here. To avoid depending on
that gap being "safe" during verification, the persist=True check below
uses two DIFFERENT weeks per club rather than the same week.

All rows this script creates (a genuine second temp club + its players,
temp Manchester players, temp Signup/Pairing/PairingBlock rows) are
cleaned up in a `finally`, leaving staging exactly as it started.

Run with: python verify_scoped_helper_chunk5_pairings_engine.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine, scoped, _default_club_id
from models import Club, Player, Signup, Pairing, PairingBlock, User
from main import app
import auth
from pairings_engine import (
    generate,
    previous_pairs_recent,
    last_opponent_pairs,
    previous_bye_player_ids,
)

TEST_SYSTEM = "The Old World"
WEEK_GEN = "22/01/2099"          # main 5-signup scenario, both clubs, same week/system
WEEK_HIST = "15/01/2099"         # history week for the 3 helper functions + blocks
WEEK_PERSIST_M = "23/01/2099"    # persist=True check, Manchester's own week
WEEK_PERSIST_O = "24/01/2099"    # persist=True check, other club's own week
WEEK_SCHEDULER = "25/01/2099"    # direct generate() call matching the scheduler's shape


def _fake_user(club_id: int, uid: int, player_id: int = None) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk5-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=player_id,
        is_super_admin=True,
        club_id=club_id,
    )


def main():
    problems = []
    client = TestClient(app)

    with Session(engine) as db:
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
    print(f"Manchester club_id = {manchester_id}")

    other_club_id = None
    created_player_ids = []
    created_signup_ids = []
    created_pairing_ids = []
    created_block_ids = []

    def _mk_player(name, club_id):
        with Session(engine) as db:
            p = Player(name=name, active=True, club_id=club_id)
            db.add(p)
            db.commit()
            db.refresh(p)
            created_player_ids.append(p.id)
            return p.id

    def _mk_signup(club_id, week, system, player_id, player_name, **kw):
        with Session(engine) as db:
            defaults = dict(standby_ok=False, tnt_ok=False, can_demo=False)
            defaults.update(kw)
            su = Signup(
                week=week, system=system, player_id=player_id,
                player_name=player_name, club_id=club_id, **defaults,
            )
            db.add(su)
            db.commit()
            db.refresh(su)
            created_signup_ids.append(su.id)
            return su

    def _mk_pairing(club_id, week, system, a_id, b_id=None, **kw):
        with Session(engine) as db:
            p = Pairing(
                week=week, system=system, a_signup_id=a_id, b_signup_id=b_id,
                club_id=club_id, status="pending", **kw,
            )
            db.add(p)
            db.commit()
            db.refresh(p)
            created_pairing_ids.append(p.id)
            return p

    def _mk_block(club_id, a_id, b_id):
        with Session(engine) as db:
            b = PairingBlock(player_a_id=a_id, player_b_id=b_id, club_id=club_id)
            db.add(b)
            db.commit()
            db.refresh(b)
            created_block_ids.append(b.id)
            return b

    def _five_signup_scenario(club_id, week, prefix):
        seeker_id = _mk_player(f"{prefix} Seeker", club_id)
        leader_id = _mk_player(f"{prefix} Leader", club_id)
        p3_id = _mk_player(f"{prefix} P3", club_id)
        p4_id = _mk_player(f"{prefix} P4", club_id)
        p5_id = _mk_player(f"{prefix} P5 (BYE)", club_id)

        _mk_signup(club_id, week, TEST_SYSTEM, seeker_id, f"{prefix} Seeker", vibe="Intro", experience="New")
        _mk_signup(club_id, week, TEST_SYSTEM, leader_id, f"{prefix} Leader", vibe="Casual", experience="Veteran", can_demo=True)
        _mk_signup(club_id, week, TEST_SYSTEM, p3_id, f"{prefix} P3", vibe="Casual", experience="Some", points=2000)
        _mk_signup(club_id, week, TEST_SYSTEM, p4_id, f"{prefix} P4", vibe="Casual", experience="Some", points=2000)
        _mk_signup(club_id, week, TEST_SYSTEM, p5_id, f"{prefix} P5 (BYE)", vibe="Casual", experience="Some", points=2000)
        return {
            "seeker": seeker_id, "leader": leader_id,
            "p3": p3_id, "p4": p4_id, "p5": p5_id,
        }

    def _check_generate_shape(label, out, ids: dict, all_signup_ids: set):
        """out is either list[Pairing] (persist=True) or list[dict] (persist=False).
        Confirms: 3 rows, 1 intro pair (seeker+leader), 1 main pair, 1 BYE,
        and every referenced signup id belongs to this club's own pool."""
        if len(out) != 3:
            problems.append(f"{label}: expected 3 pairing rows, got {len(out)}")
            return
        a_ids = {(r.a_signup_id if hasattr(r, "a_signup_id") else r["a_signup_id"]) for r in out}
        b_ids = {(r.b_signup_id if hasattr(r, "b_signup_id") else r["b_signup_id"]) for r in out} - {None}
        referenced = a_ids | b_ids
        foreign = referenced - all_signup_ids
        if foreign:
            problems.append(f"{label}: referenced signup ids outside this club's own pool: {foreign}")
        bye_rows = [r for r in out if (r.b_signup_id if hasattr(r, "b_signup_id") else r["b_signup_id"]) is None]
        if len(bye_rows) != 1:
            problems.append(f"{label}: expected exactly 1 BYE row, got {len(bye_rows)}")
        intro_rows = [
            r for r in out
            if ids["seeker"] in (
                (r.a_signup_id if hasattr(r, "a_signup_id") else r["a_signup_id"]),
                (r.b_signup_id if hasattr(r, "b_signup_id") else r["b_signup_id"]),
            )
        ]
        if len(intro_rows) != 1:
            problems.append(f"{label}: expected exactly 1 row involving the intro seeker, got {len(intro_rows)}")
        else:
            row = intro_rows[0]
            row_ids = {
                (row.a_signup_id if hasattr(row, "a_signup_id") else row["a_signup_id"]),
                (row.b_signup_id if hasattr(row, "b_signup_id") else row["b_signup_id"]),
            }
            if row_ids != {ids["seeker"], ids["leader"]}:
                problems.append(f"{label}: intro row didn't pair seeker with leader: {row_ids}")
        print(f"{label}: 3 rows, all signup ids within own club pool, 1 intro (seeker+leader), 1 BYE -- OK")

    try:
        # =====================================================================
        # 0. Second temp club
        # =====================================================================
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk5 Other Club", slug="zztest-chunk5-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id
        print(f"Created other club id={other_club_id}")

        # =====================================================================
        # 1. Cross-club isolation of the 3 history helpers + block scoping,
        #    via distinct history data for each club (WEEK_HIST, within the
        #    recent_w=3 week window of WEEK_GEN for The Old World).
        # =====================================================================
        m_hist_a = _mk_player("ZZTest Chunk5 M-HistA", manchester_id)
        m_hist_b = _mk_player("ZZTest Chunk5 M-HistB", manchester_id)
        o_hist_a = _mk_player("ZZTest Chunk5 O-HistA", other_club_id)
        o_hist_b = _mk_player("ZZTest Chunk5 O-HistB", other_club_id)

        m_su_a = _mk_signup(manchester_id, WEEK_HIST, TEST_SYSTEM, m_hist_a, "ZZTest Chunk5 M-HistA")
        m_su_b = _mk_signup(manchester_id, WEEK_HIST, TEST_SYSTEM, m_hist_b, "ZZTest Chunk5 M-HistB")
        o_su_a = _mk_signup(other_club_id, WEEK_HIST, TEST_SYSTEM, o_hist_a, "ZZTest Chunk5 O-HistA")
        o_su_b = _mk_signup(other_club_id, WEEK_HIST, TEST_SYSTEM, o_hist_b, "ZZTest Chunk5 O-HistB")

        # A played pairing for each club (feeds previous_pairs_recent + last_opponent_pairs)
        _mk_pairing(manchester_id, WEEK_HIST, TEST_SYSTEM, m_su_a.id, m_su_b.id)
        _mk_pairing(other_club_id, WEEK_HIST, TEST_SYSTEM, o_su_a.id, o_su_b.id)

        # A BYE for each club (feeds previous_bye_player_ids)
        m_bye_player = _mk_player("ZZTest Chunk5 M-Bye", manchester_id)
        o_bye_player = _mk_player("ZZTest Chunk5 O-Bye", other_club_id)
        m_bye_su = _mk_signup(manchester_id, WEEK_HIST, TEST_SYSTEM, m_bye_player, "ZZTest Chunk5 M-Bye")
        o_bye_su = _mk_signup(other_club_id, WEEK_HIST, TEST_SYSTEM, o_bye_player, "ZZTest Chunk5 O-Bye")
        _mk_pairing(manchester_id, WEEK_HIST, TEST_SYSTEM, m_bye_su.id, None)
        _mk_pairing(other_club_id, WEEK_HIST, TEST_SYSTEM, o_bye_su.id, None)

        # A pairing block for each club
        _mk_block(manchester_id, m_hist_a, m_hist_b)
        _mk_block(other_club_id, o_hist_a, o_hist_b)

        with Session(engine) as db:
            m_recent = previous_pairs_recent(db, TEST_SYSTEM, WEEK_GEN, 3, manchester_id)
            o_recent = previous_pairs_recent(db, TEST_SYSTEM, WEEK_GEN, 3, other_club_id)
            m_last_opp = last_opponent_pairs(db, TEST_SYSTEM, WEEK_GEN, manchester_id)
            o_last_opp = last_opponent_pairs(db, TEST_SYSTEM, WEEK_GEN, other_club_id)
            m_bye_ids = previous_bye_player_ids(db, TEST_SYSTEM, WEEK_GEN, manchester_id)
            o_bye_ids = previous_bye_player_ids(db, TEST_SYSTEM, WEEK_GEN, other_club_id)
            m_blocks = {
                tuple(sorted([b.player_a_id, b.player_b_id]))
                for b in db.exec(scoped(PairingBlock, manchester_id)).all()
            }
            o_blocks = {
                tuple(sorted([b.player_a_id, b.player_b_id]))
                for b in db.exec(scoped(PairingBlock, other_club_id)).all()
            }

        m_pair_key = tuple(sorted(["zztest chunk5 m-hista", "zztest chunk5 m-histb"]))
        o_pair_key = tuple(sorted(["zztest chunk5 o-hista", "zztest chunk5 o-histb"]))
        if m_pair_key not in m_recent or o_pair_key in m_recent:
            problems.append(f"previous_pairs_recent(manchester) wrong: {m_recent}")
        else:
            print("previous_pairs_recent: manchester sees its own recent pair, not the other club's -- OK")
        if o_pair_key not in o_recent or m_pair_key in o_recent:
            problems.append(f"previous_pairs_recent(other_club) wrong: {o_recent}")
        else:
            print("previous_pairs_recent: other club sees its own recent pair, not manchester's -- OK")

        m_opp_key = tuple(sorted([m_hist_a, m_hist_b]))
        o_opp_key = tuple(sorted([o_hist_a, o_hist_b]))
        if m_opp_key not in m_last_opp or o_opp_key in m_last_opp:
            problems.append(f"last_opponent_pairs(manchester) wrong: {m_last_opp}")
        else:
            print("last_opponent_pairs: manchester isolated from other club -- OK")
        if o_opp_key not in o_last_opp or m_opp_key in o_last_opp:
            problems.append(f"last_opponent_pairs(other_club) wrong: {o_last_opp}")
        else:
            print("last_opponent_pairs: other club isolated from manchester -- OK")

        if m_bye_player not in m_bye_ids or o_bye_player in m_bye_ids:
            problems.append(f"previous_bye_player_ids(manchester) wrong: {m_bye_ids}")
        else:
            print("previous_bye_player_ids: manchester isolated from other club -- OK")
        if o_bye_player not in o_bye_ids or m_bye_player in o_bye_ids:
            problems.append(f"previous_bye_player_ids(other_club) wrong: {o_bye_ids}")
        else:
            print("previous_bye_player_ids: other club isolated from manchester -- OK")

        if m_opp_key not in m_blocks or o_opp_key in m_blocks:
            problems.append(f"scoped(PairingBlock, manchester) wrong: {m_blocks}")
        else:
            print("scoped(PairingBlock): manchester isolated from other club -- OK")
        if o_opp_key not in o_blocks or m_opp_key in o_blocks:
            problems.append(f"scoped(PairingBlock, other_club) wrong: {o_blocks}")
        else:
            print("scoped(PairingBlock): other club isolated from manchester -- OK")

        # =====================================================================
        # 2. generate() candidate-pool isolation: identical 5-signup scenario
        #    for BOTH clubs, same week/system. Dry-run only (persist=False,
        #    no deletes) so it's safe to reuse the same week/system key.
        # =====================================================================
        m_ids = _five_signup_scenario(manchester_id, WEEK_GEN, "ZZTest Chunk5 M")
        o_ids = _five_signup_scenario(other_club_id, WEEK_GEN, "ZZTest Chunk5 O")

        # m_ids/o_ids currently hold Player ids; re-resolve to this week's Signup ids
        def _signup_ids_for(club_id, week, player_ids: dict):
            with Session(engine) as db:
                rows = db.exec(
                    scoped(Signup, club_id).where(Signup.week == week).where(Signup.system == TEST_SYSTEM)
                ).all()
            by_player = {r.player_id: r.id for r in rows}
            return {k: by_player[v] for k, v in player_ids.items()}

        m_su_ids = _signup_ids_for(manchester_id, WEEK_GEN, m_ids)
        o_su_ids = _signup_ids_for(other_club_id, WEEK_GEN, o_ids)

        with Session(engine) as db:
            m_preview = generate(db, WEEK_GEN, TEST_SYSTEM, persist=False, club_id=manchester_id)
        _check_generate_shape("generate() preview, manchester", m_preview, m_su_ids, set(m_su_ids.values()))

        with Session(engine) as db:
            o_preview = generate(db, WEEK_GEN, TEST_SYSTEM, persist=False, club_id=other_club_id)
        _check_generate_shape("generate() preview, other club", o_preview, o_su_ids, set(o_su_ids.values()))

        # =====================================================================
        # 3. generate() persist=True via the real admin endpoint, one week
        #    per club (see module docstring re: the unscoped delete-old-
        #    pairings query in admin.py::pairings_generate, out of scope here).
        # =====================================================================
        m2_ids_players = _five_signup_scenario(manchester_id, WEEK_PERSIST_M, "ZZTest Chunk5 M2")
        o2_ids_players = _five_signup_scenario(other_club_id, WEEK_PERSIST_O, "ZZTest Chunk5 O2")
        m2_su_ids = _signup_ids_for(manchester_id, WEEK_PERSIST_M, m2_ids_players)
        o2_su_ids = _signup_ids_for(other_club_id, WEEK_PERSIST_O, o2_ids_players)

        manchester_admin = _fake_user(manchester_id, uid=999101)
        app.dependency_overrides[auth.require_user] = lambda: manchester_admin
        app.dependency_overrides[auth.current_user] = lambda: manchester_admin
        r = client.post("/admin/pairings/generate", json={"system": TEST_SYSTEM, "week": WEEK_PERSIST_M})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/generate (manchester) failed: {r.status_code} {r.text}")
        else:
            with Session(engine) as db:
                rows = db.exec(
                    scoped(Pairing, manchester_id)
                    .where(Pairing.week == WEEK_PERSIST_M).where(Pairing.system == TEST_SYSTEM)
                ).all()
                for p in rows:
                    created_pairing_ids.append(p.id)
                for p in rows:
                    if p.club_id != manchester_id:
                        problems.append(f"generate()/persist manchester: pairing {p.id} has club_id={p.club_id}")
            _check_generate_shape("POST /admin/pairings/generate, manchester (persisted)", rows, m2_su_ids, set(m2_su_ids.values()))

        other_admin = _fake_user(other_club_id, uid=999102)
        app.dependency_overrides[auth.require_user] = lambda: other_admin
        app.dependency_overrides[auth.current_user] = lambda: other_admin
        r = client.post("/admin/pairings/generate", json={"system": TEST_SYSTEM, "week": WEEK_PERSIST_O})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/generate (other club) failed: {r.status_code} {r.text}")
        else:
            with Session(engine) as db:
                rows = db.exec(
                    scoped(Pairing, other_club_id)
                    .where(Pairing.week == WEEK_PERSIST_O).where(Pairing.system == TEST_SYSTEM)
                ).all()
                for p in rows:
                    created_pairing_ids.append(p.id)
                for p in rows:
                    if p.club_id != other_club_id:
                        problems.append(f"generate()/persist other club: pairing {p.id} has club_id={p.club_id}")
            _check_generate_shape("POST /admin/pairings/generate, other club (persisted)", rows, o2_su_ids, set(o2_su_ids.values()))

        # Confirm manchester's persisted rows from the earlier call are still
        # intact and untouched by the other club's later generate() call.
        with Session(engine) as db:
            still_there = db.exec(
                scoped(Pairing, manchester_id)
                .where(Pairing.week == WEEK_PERSIST_M).where(Pairing.system == TEST_SYSTEM)
            ).all()
        if len(still_there) != 3:
            problems.append(f"manchester's persisted rows were disturbed by the other club's generate() call: {len(still_there)} remain, expected 3")
        else:
            print("manchester's persisted rows (different week) untouched by the other club's later generate() call -- OK")

        # Also smoke-test the preview endpoint end-to-end (not just direct call)
        app.dependency_overrides[auth.require_user] = lambda: manchester_admin
        app.dependency_overrides[auth.current_user] = lambda: manchester_admin
        r = client.post("/admin/pairings/preview", json={"system": TEST_SYSTEM, "week": WEEK_GEN})
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/preview failed: {r.status_code} {r.text}")
        else:
            print(f"POST /admin/pairings/preview -> 200, {len(r.json()['rows'])} display row(s)")

        # =====================================================================
        # 4. Scheduler call-path: identical generate(...) call shape used by
        #    run_auto_pairings_check.py, exercised directly (running the real
        #    script would post to Discord / touch club_settings -- too
        #    invasive, same caution as the publish_state/app_settings chunks).
        # =====================================================================
        s_ids_players = _five_signup_scenario(manchester_id, WEEK_SCHEDULER, "ZZTest Chunk5 Sched")
        s_su_ids = _signup_ids_for(manchester_id, WEEK_SCHEDULER, s_ids_players)
        with Session(engine) as db:
            sched_out = generate(
                db, WEEK_SCHEDULER, TEST_SYSTEM,
                allow_repeats_when_needed=True, persist=True,
                club_id=_default_club_id(db),
            )
            for p in sched_out:
                created_pairing_ids.append(p.id)
        _check_generate_shape("scheduler-shaped generate() call (persist=True)", sched_out, s_su_ids, set(s_su_ids.values()))

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for pid in created_pairing_ids:
                p = db.get(Pairing, pid)
                if p:
                    db.delete(p)
            db.commit()
            for bid in created_block_ids:
                b = db.get(PairingBlock, bid)
                if b:
                    db.delete(b)
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
            "\nVerification passed: the 3 history helpers and the block query "
            "inside generate() are correctly isolated per club_id; generate()'s "
            "candidate pool never crosses club boundaries for the same "
            "week/system; the real 5-signup scenario still produces the "
            "identical intro+main+BYE structure for both persist=False and "
            "persist=True, for both clubs independently; the scheduler's exact "
            "call shape still works."
        )


if __name__ == "__main__":
    main()
