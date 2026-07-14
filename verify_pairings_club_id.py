"""One-off verification script for the pairings.club_id dual-run (Phase 1,
table 7 of 10). Not part of the app; run manually against staging.

Exercises all eight real Pairing-creating write sites through FastAPI
TestClient, against the real staging DB, with only the auth dependency
overridden (in-memory fake user(s), no real User row touched):

    signups.py::drop_signup          (1 BYE pairing)
    signups.py::submit_prearranged   (1 pairing)
    signups.py::swap_signups         (3 pairings: X-Y, BYE-Z, BYE-W)
    pairings_engine.py::generate()   (3 sites: intro pre-pass, main match, BYE),
                                      exercised via POST /admin/pairings/generate

Creates temporary Player rows (never touches the two real staging players)
so scenarios needing 2-5 distinct players don't depend on staging's small
real player pool. Uses far-future test weeks so test data can never collide
with real signups/pairings. Deletes every row it creates at the end, in a
`finally`, so staging is left exactly as it started.

Run with: python verify_pairings_club_id.py [--post-contract]
--post-contract also re-hits the read/preview endpoints that matter after
the NOT NULL contract step, and skips nothing (all 8 sites still exercised).
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Signup, Pairing, Club, Player, User, PublishState
from main import app
import auth

WEEK_GENERATE = "01/01/2099"
WEEK_PREARRANGED = "01/01/2099"
WEEK_SWAP = "01/01/2099"
WEEK_DROP = "02/01/2099"


def _fake_user(player_id: int, club_id: int, uid: int = 999999) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-pairings-{uid}",
        discord_name="Verify Script",
        player_id=player_id,
        is_super_admin=True,
        club_id=club_id,
    )


def main():
    post_contract = "--post-contract" in sys.argv

    with Session(engine) as db:
        manchester_id = db.exec(select(Club).where(Club.slug == "manchester")).first().id
    print(f"Manchester club_id = {manchester_id}")

    created_player_ids = []
    created_signup_ids = []
    created_pairing_ids = []
    created_publish_state_ids = []
    problems = []

    def _mk_player(name):
        with Session(engine) as db:
            p = Player(name=name, active=True, club_id=manchester_id)
            db.add(p)
            db.commit()
            db.refresh(p)
            created_player_ids.append(p.id)
            return p.id

    def _mk_signup(week, system, player_id, player_name, **kw):
        with Session(engine) as db:
            defaults = dict(standby_ok=False, tnt_ok=False, can_demo=False)
            defaults.update(kw)
            su = Signup(
                week=week, system=system,
                player_id=player_id, player_name=player_name,
                club_id=manchester_id,
                **defaults,
            )
            db.add(su)
            db.commit()
            db.refresh(su)
            created_signup_ids.append(su.id)
            return su

    def _mk_pairing(week, system, a_id, b_id=None, **kw):
        with Session(engine) as db:
            p = Pairing(
                week=week, system=system,
                a_signup_id=a_id, b_signup_id=b_id,
                club_id=manchester_id,
                status="pending", **kw,
            )
            db.add(p)
            db.commit()
            db.refresh(p)
            created_pairing_ids.append(p.id)
            return p

    def _mk_publish_state(week, system):
        with Session(engine) as db:
            ps = PublishState(week=week, system=system, published=True, club_id=manchester_id)
            db.add(ps)
            db.commit()
            db.refresh(ps)
            created_publish_state_ids.append(ps.id)
            return ps

    def _check_pairing_club_id(label, pairing_id):
        with Session(engine) as db:
            p = db.get(Pairing, pairing_id)
            print(f"  {label}: pairing id={p.id} club_id={p.club_id}")
            if p.club_id != manchester_id:
                problems.append(f"{label}: club_id={p.club_id}, expected {manchester_id}")

    client = TestClient(app)

    try:
        # ---------------------------------------------------------------
        # 1-3. pairings_engine.py::generate() — intro pre-pass, main match,
        # BYE fallback. Exercised via POST /admin/pairings/generate.
        # ---------------------------------------------------------------
        seeker_id = _mk_player("ZZTest Seeker")
        leader_id = _mk_player("ZZTest Leader")
        p3_id = _mk_player("ZZTest P3")
        p4_id = _mk_player("ZZTest P4")
        p5_id = _mk_player("ZZTest P5 (BYE)")

        _mk_signup(WEEK_GENERATE, "The Old World", seeker_id, "ZZTest Seeker", vibe="Intro", experience="New")
        _mk_signup(WEEK_GENERATE, "The Old World", leader_id, "ZZTest Leader", vibe="Casual", experience="Veteran", can_demo=True)
        _mk_signup(WEEK_GENERATE, "The Old World", p3_id, "ZZTest P3", vibe="Casual", experience="Some", points=2000)
        _mk_signup(WEEK_GENERATE, "The Old World", p4_id, "ZZTest P4", vibe="Casual", experience="Some", points=2000)
        _mk_signup(WEEK_GENERATE, "The Old World", p5_id, "ZZTest P5 (BYE)", vibe="Casual", experience="Some", points=2000)

        fake_admin = _fake_user(seeker_id, manchester_id, uid=999001)
        app.dependency_overrides[auth.require_user] = lambda: fake_admin
        app.dependency_overrides[auth.current_user] = lambda: fake_admin

        r = client.post("/admin/pairings/generate", json={"system": "The Old World", "week": WEEK_GENERATE})
        print("POST /admin/pairings/generate ->", r.status_code)
        if r.status_code != 200:
            problems.append(f"POST /admin/pairings/generate failed: {r.status_code} {r.text}")
        else:
            with Session(engine) as db:
                rows = db.exec(
                    select(Pairing)
                    .where(Pairing.week == WEEK_GENERATE)
                    .where(Pairing.system == "The Old World")
                ).all()
                for p in rows:
                    created_pairing_ids.append(p.id)
                print(f"  generate() produced {len(rows)} pairing row(s):")
                for p in rows:
                    print(f"    id={p.id} a={p.a_signup_id} b={p.b_signup_id} club_id={p.club_id}")
                    if p.club_id != manchester_id:
                        problems.append(f"generate() pairing id={p.id}: club_id={p.club_id}, expected {manchester_id}")
                if len(rows) != 3:
                    problems.append(f"generate() expected 3 pairing rows (intro + main + BYE), got {len(rows)}")
                bye_rows = [p for p in rows if p.b_signup_id is None]
                if len(bye_rows) != 1:
                    problems.append(f"generate() expected exactly 1 BYE row, got {len(bye_rows)}")

        # ---------------------------------------------------------------
        # 4. signups.py::submit_prearranged
        # ---------------------------------------------------------------
        pa_id = _mk_player("ZZTest PrearrangedA")
        pb_id = _mk_player("ZZTest PrearrangedB")
        fake_pa = _fake_user(pa_id, manchester_id, uid=999002)
        app.dependency_overrides[auth.require_user] = lambda: fake_pa
        app.dependency_overrides[auth.current_user] = lambda: fake_pa

        r = client.post("/signups/prearranged", json={
            "system": "Kill Team",
            "week": WEEK_PREARRANGED,
            "player_a_id": pa_id,
            "player_b_id": pb_id,
            "faction_a": "Death Korps",
            "faction_b": "Farstalker Kinband",
        })
        print("POST /signups/prearranged ->", r.status_code)
        if r.status_code != 200:
            problems.append(f"POST /signups/prearranged failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            created_signup_ids += [body["signup_a"]["id"], body["signup_b"]["id"]]
            pairing_id = body["pairing"]["id"]
            created_pairing_ids.append(pairing_id)
            _check_pairing_club_id("submit_prearranged", pairing_id)

        # ---------------------------------------------------------------
        # 5-6. signups.py::drop_signup (BYE pairing for displaced opponent)
        # ---------------------------------------------------------------
        da_id = _mk_player("ZZTest DropA")
        db_id = _mk_player("ZZTest DropB")
        su_da = _mk_signup(WEEK_DROP, "Kill Team", da_id, "ZZTest DropA")
        su_db = _mk_signup(WEEK_DROP, "Kill Team", db_id, "ZZTest DropB")
        drop_pairing = _mk_pairing(WEEK_DROP, "Kill Team", su_da.id, su_db.id)
        _mk_publish_state(WEEK_DROP, "Kill Team")

        fake_da = _fake_user(da_id, manchester_id, uid=999003)
        app.dependency_overrides[auth.require_user] = lambda: fake_da
        app.dependency_overrides[auth.current_user] = lambda: fake_da

        r = client.delete("/signups/mine", params={"system": "Kill Team", "week": WEEK_DROP})
        print("DELETE /signups/mine ->", r.status_code, r.json() if r.status_code == 200 else r.text)
        if r.status_code != 200:
            problems.append(f"DELETE /signups/mine failed: {r.status_code} {r.text}")
        else:
            with Session(engine) as db:
                bye_rows = db.exec(
                    select(Pairing)
                    .where(Pairing.week == WEEK_DROP)
                    .where(Pairing.system == "Kill Team")
                    .where(Pairing.a_signup_id == su_db.id)
                    .where(Pairing.id != drop_pairing.id)
                ).all()
                if len(bye_rows) != 1:
                    problems.append(f"drop_signup: expected 1 new BYE pairing for DropB, found {len(bye_rows)}")
                else:
                    created_pairing_ids.append(bye_rows[0].id)
                    _check_pairing_club_id("drop_signup BYE", bye_rows[0].id)
            # original pairing was deleted by drop_signup itself; drop from our cleanup list
            if drop_pairing.id in created_pairing_ids:
                created_pairing_ids.remove(drop_pairing.id)
            # su_da was deleted by drop_signup; drop from our cleanup list
            if su_da.id in created_signup_ids:
                created_signup_ids.remove(su_da.id)

        # ---------------------------------------------------------------
        # 7-9. signups.py::swap_signups (X-Y prearranged + 2 BYEs)
        # ---------------------------------------------------------------
        x_id = _mk_player("ZZTest SwapX")
        y_id = _mk_player("ZZTest SwapY")
        z_id = _mk_player("ZZTest SwapZ")
        w_id = _mk_player("ZZTest SwapW")
        su_x = _mk_signup(WEEK_SWAP, "The Horus Heresy", x_id, "ZZTest SwapX")
        su_y = _mk_signup(WEEK_SWAP, "The Horus Heresy", y_id, "ZZTest SwapY")
        su_z = _mk_signup(WEEK_SWAP, "The Horus Heresy", z_id, "ZZTest SwapZ")
        su_w = _mk_signup(WEEK_SWAP, "The Horus Heresy", w_id, "ZZTest SwapW")
        xz_pairing = _mk_pairing(WEEK_SWAP, "The Horus Heresy", su_x.id, su_z.id)
        yw_pairing = _mk_pairing(WEEK_SWAP, "The Horus Heresy", su_y.id, su_w.id)
        _mk_publish_state(WEEK_SWAP, "The Horus Heresy")

        fake_x = _fake_user(x_id, manchester_id, uid=999004)
        app.dependency_overrides[auth.require_user] = lambda: fake_x
        app.dependency_overrides[auth.current_user] = lambda: fake_x

        r = client.post("/signups/swap", json={
            "system": "The Horus Heresy",
            "week": WEEK_SWAP,
            "opponent_player_id": y_id,
        })
        print("POST /signups/swap ->", r.status_code, r.text if r.status_code != 200 else r.json())
        if r.status_code != 200:
            problems.append(f"POST /signups/swap failed: {r.status_code} {r.text}")
        else:
            with Session(engine) as db:
                rows = db.exec(
                    select(Pairing)
                    .where(Pairing.week == WEEK_SWAP)
                    .where(Pairing.system == "The Horus Heresy")
                    .where(Pairing.id.not_in([xz_pairing.id, yw_pairing.id]))
                ).all()
                print(f"  swap produced {len(rows)} new pairing row(s):")
                for p in rows:
                    created_pairing_ids.append(p.id)
                    print(f"    id={p.id} a={p.a_signup_id} b={p.b_signup_id} club_id={p.club_id}")
                    if p.club_id != manchester_id:
                        problems.append(f"swap_signups pairing id={p.id}: club_id={p.club_id}, expected {manchester_id}")
                if len(rows) != 3:
                    problems.append(f"swap_signups: expected 3 new pairings (X-Y + 2 BYE), got {len(rows)}")
            # xz_pairing and yw_pairing were deleted by swap_signups
            for pid in (xz_pairing.id, yw_pairing.id):
                if pid in created_pairing_ids:
                    created_pairing_ids.remove(pid)

        if post_contract:
            r = client.get("/pairings", params={"system": "Kill Team", "week": WEEK_PREARRANGED})
            print("GET /pairings ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /pairings failed: {r.status_code} {r.text}")

            r = client.post("/admin/pairings/preview", json={"system": "The Old World", "week": WEEK_GENERATE})
            print("POST /admin/pairings/preview ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"POST /admin/pairings/preview failed: {r.status_code} {r.text}")

            r = client.get("/admin/pairings", params={"system": "Kill Team", "week": WEEK_PREARRANGED})
            print("GET /admin/pairings ->", r.status_code)
            if r.status_code != 200:
                problems.append(f"GET /admin/pairings failed: {r.status_code} {r.text}")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for pid in created_pairing_ids:
                p = db.get(Pairing, pid)
                if p:
                    db.delete(p)
            for sid in created_signup_ids:
                su = db.get(Signup, sid)
                if su:
                    db.delete(su)
            for psid in created_publish_state_ids:
                ps = db.get(PublishState, psid)
                if ps:
                    db.delete(ps)
            for plid in created_player_ids:
                pl = db.get(Player, plid)
                if pl:
                    db.delete(pl)
            db.commit()
            print(
                f"Cleaned up {len(created_pairing_ids)} pairing(s), "
                f"{len(created_signup_ids)} signup(s), "
                f"{len(created_publish_state_ids)} publish_state(s), "
                f"{len(created_player_ids)} player(s)."
            )

    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
