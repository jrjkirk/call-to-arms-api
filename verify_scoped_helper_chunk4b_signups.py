"""One-off verification script for the scoped-query-helper phase, chunk 4b
(signups.py part 2: drop_signup, submit_prearranged, swap_signups,
_get_all_byes). Not part of the app; run manually against staging.

Proves, beyond "no errors":
  1. submit_prearranged() — the important fix — 404s when player_a_id or
     player_b_id belongs to a genuine second temp club, with ZERO Signup/
     Pairing rows created on the rejected attempt (not just a 404 status).
     Same-club prearranged game still works, all three created rows get
     club_id = caller's real club_id.
  2. swap_signups() — a player_1_id / opponent_player_id belonging to the
     other club correctly 422s ("not signed up") via the scoped x_signup/
     y_signup lookup, proving the "no separate ownership check needed"
     reasoning holds in practice. Same-club swap (both self-serve and
     admin-acting-for-another-player) still works, all new Pairing rows
     get the caller's real club_id.
  3. drop_signup() — with a second club's identically-shaped signup/pairing
     data present, dropping a Manchester player only touches Manchester's
     rows, in both the pre-publish and post-publish branches.
  4. _get_all_byes() returns only the caller's club's BYE list, confirmed
     with a second club's BYE data present and excluded.

All rows this script creates (a genuine second temp club + its player(s),
temp Manchester players, temp Signup/Pairing/PublishState rows) are cleaned
up in a `finally`, leaving staging exactly as it started.

Run with: python verify_scoped_helper_chunk4b_signups.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, Signup, Pairing, PublishState, Player, User
from main import app
import auth
from signups import _get_all_byes

TEST_SYSTEM = "The Old World"
WEEK_PREARRANGED_OK = "10/01/2099"
WEEK_PREARRANGED_BAD_A = "11/01/2099"
WEEK_PREARRANGED_BAD_B = "12/01/2099"
WEEK_SWAP = "13/01/2099"
WEEK_SWAP_ADMIN = "13/02/2099"
WEEK_DROP_PRE = "14/01/2099"
WEEK_DROP_POST = "15/01/2099"
WEEK_BYES = "16/01/2099"


def _fake_user(club_id: int, uid: int, player_id: int, is_super_admin: bool = False) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk4b-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=player_id,
        is_super_admin=is_super_admin,
        club_id=club_id,
    )


def main():
    problems = []
    client = TestClient(app)

    with Session(engine) as db:
        manchester = db.exec(select(Club).where(Club.slug == "manchester")).first()
        manchester_id = manchester.id
    print(f"Manchester club_id = {manchester_id}")

    other_club_id = None
    created_player_ids = []
    created_signup_ids = []
    created_pairing_ids = []
    created_publish_ids = []

    def count_signups_for_week(week):
        with Session(engine) as db:
            return len(db.exec(select(Signup).where(Signup.week == week)).all())

    def count_pairings_for_week(week):
        with Session(engine) as db:
            return len(db.exec(select(Pairing).where(Pairing.week == week)).all())

    try:
        # --- Second club + its own players, inserted directly ---
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk4b Other Club", slug="zztest-chunk4b-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id

            other_player_a = Player(name="ZZTest Chunk4b Other Player A", club_id=other_club_id, active=True)
            other_player_b = Player(name="ZZTest Chunk4b Other Player B", club_id=other_club_id, active=True)
            db.add(other_player_a)
            db.add(other_player_b)
            db.commit()
            db.refresh(other_player_a)
            db.refresh(other_player_b)
            created_player_ids.extend([other_player_a.id, other_player_b.id])
            other_player_a_id = other_player_a.id
            other_player_b_id = other_player_b.id
        print(f"Created other club id={other_club_id} with players id={other_player_a_id},{other_player_b_id}")

        # --- Manchester temp players ---
        with Session(engine) as db:
            m_players = []
            for i in range(4):
                p = Player(name=f"ZZTest Chunk4b Manchester Player {i}", club_id=manchester_id, active=True)
                db.add(p)
                m_players.append(p)
            db.commit()
            for p in m_players:
                db.refresh(p)
                created_player_ids.append(p.id)
            m_ids = [p.id for p in m_players]
        print(f"Created Manchester players id={m_ids}")
        m_a, m_b, m_c, m_d = m_ids

        manchester_caller = _fake_user(manchester_id, uid=999995, player_id=m_a, is_super_admin=True)
        app.dependency_overrides[auth.current_user] = lambda: manchester_caller
        app.dependency_overrides[auth.require_user] = lambda: manchester_caller

        # =====================================================================
        # 1a. submit_prearranged — player_a_id belongs to the other club -> 404,
        #     zero rows created
        # =====================================================================
        before_signups = count_signups_for_week(WEEK_PREARRANGED_BAD_A)
        before_pairings = count_pairings_for_week(WEEK_PREARRANGED_BAD_A)
        resp = client.post("/signups/prearranged", json={
            "system": TEST_SYSTEM, "week": WEEK_PREARRANGED_BAD_A,
            "player_a_id": other_player_a_id, "player_b_id": m_b,
            "faction_a": "Empire", "faction_b": "Dwarfs",
        })
        after_signups = count_signups_for_week(WEEK_PREARRANGED_BAD_A)
        after_pairings = count_pairings_for_week(WEEK_PREARRANGED_BAD_A)
        if resp.status_code != 404:
            problems.append(f"prearranged (foreign player_a_id) expected 404, got {resp.status_code} {resp.text}")
        else:
            print(f"prearranged (foreign player_a_id) -> 404 ({resp.json().get('detail')})")
        if after_signups != before_signups or after_pairings != before_pairings:
            problems.append(
                f"prearranged (foreign player_a_id) created rows despite 404: "
                f"signups {before_signups}->{after_signups}, pairings {before_pairings}->{after_pairings}"
            )
        else:
            print(f"prearranged (foreign player_a_id) -> zero rows created (signups={after_signups}, pairings={after_pairings})")

        # =====================================================================
        # 1b. submit_prearranged — player_b_id belongs to the other club -> 404,
        #     zero rows created
        # =====================================================================
        before_signups = count_signups_for_week(WEEK_PREARRANGED_BAD_B)
        before_pairings = count_pairings_for_week(WEEK_PREARRANGED_BAD_B)
        resp = client.post("/signups/prearranged", json={
            "system": TEST_SYSTEM, "week": WEEK_PREARRANGED_BAD_B,
            "player_a_id": m_b, "player_b_id": other_player_b_id,
            "faction_a": "Empire", "faction_b": "Dwarfs",
        })
        after_signups = count_signups_for_week(WEEK_PREARRANGED_BAD_B)
        after_pairings = count_pairings_for_week(WEEK_PREARRANGED_BAD_B)
        if resp.status_code != 404:
            problems.append(f"prearranged (foreign player_b_id) expected 404, got {resp.status_code} {resp.text}")
        else:
            print(f"prearranged (foreign player_b_id) -> 404 ({resp.json().get('detail')})")
        if after_signups != before_signups or after_pairings != before_pairings:
            problems.append(
                f"prearranged (foreign player_b_id) created rows despite 404: "
                f"signups {before_signups}->{after_signups}, pairings {before_pairings}->{after_pairings}"
            )
        else:
            print(f"prearranged (foreign player_b_id) -> zero rows created (signups={after_signups}, pairings={after_pairings})")

        # =====================================================================
        # 1c. submit_prearranged — same-club game still works, club_id=manchester
        #     on all three created rows
        # =====================================================================
        resp = client.post("/signups/prearranged", json={
            "system": TEST_SYSTEM, "week": WEEK_PREARRANGED_OK,
            "player_a_id": m_b, "player_b_id": m_c,
            "faction_a": "Empire", "faction_b": "Dwarfs",
        })
        if resp.status_code != 200:
            problems.append(f"prearranged (same-club) expected 200, got {resp.status_code} {resp.text}")
        else:
            j = resp.json()
            su_a_id = j["signup_a"]["id"]
            su_b_id = j["signup_b"]["id"]
            pairing_id = j["pairing"]["id"]
            created_signup_ids.extend([su_a_id, su_b_id])
            created_pairing_ids.append(pairing_id)
            if j["signup_a"]["club_id"] != manchester_id or j["signup_b"]["club_id"] != manchester_id or j["pairing"]["club_id"] != manchester_id:
                problems.append(f"prearranged (same-club) club_id mismatch: {j['signup_a']['club_id']}, {j['signup_b']['club_id']}, {j['pairing']['club_id']}")
            else:
                print(f"prearranged (same-club) -> 200, su_a={su_a_id}, su_b={su_b_id}, pairing={pairing_id}, all club_id={manchester_id}")

        # =====================================================================
        # 2. swap_signups — cross-club and same-club
        # =====================================================================
        # Set up X (m_a) paired with Z (m_b), and Y (m_d) paired with W (m_c),
        # plus a PublishState gate. This shape lets a real X<->Y swap happen
        # (X and Y are NOT already paired with each other).
        def _signup_pair(db, week, a_player, a_name, b_player, b_name):
            a = Signup(week=week, system=TEST_SYSTEM, player_id=a_player, player_name=a_name, club_id=manchester_id)
            b = Signup(week=week, system=TEST_SYSTEM, player_id=b_player, player_name=b_name, club_id=manchester_id)
            db.add(a)
            db.add(b)
            db.commit()
            db.refresh(a)
            db.refresh(b)
            pairing = Pairing(
                week=week, system=TEST_SYSTEM,
                a_signup_id=a.id, b_signup_id=b.id,
                status="pending", prearranged=False,
                a_faction=None, b_faction=None, club_id=manchester_id,
            )
            db.add(pairing)
            db.commit()
            db.refresh(pairing)
            return a.id, b.id, pairing.id

        with Session(engine) as db:
            x_signup_id, z_signup_id, xz_pairing_id = _signup_pair(db, WEEK_SWAP, m_a, "X", m_b, "Z")
            y_signup_id, w_signup_id, yw_pairing_id = _signup_pair(db, WEEK_SWAP, m_d, "Y", m_c, "W")
            created_signup_ids.extend([x_signup_id, z_signup_id, y_signup_id, w_signup_id])
            created_pairing_ids.extend([xz_pairing_id, yw_pairing_id])

            gate = PublishState(week=WEEK_SWAP, system=TEST_SYSTEM, published=True, club_id=manchester_id)
            db.add(gate)
            db.commit()
            db.refresh(gate)
            created_publish_ids.append(gate.id)
        print(f"Set up swap scenario: X(m_a)={x_signup_id} vs Z(m_b)={z_signup_id}, Y(m_d)={y_signup_id} vs W(m_c)={w_signup_id}, gate published")

        # 2a. opponent_player_id belongs to the other club -> 422, not a leak
        resp = client.post("/signups/swap", json={
            "system": TEST_SYSTEM, "week": WEEK_SWAP,
            "opponent_player_id": other_player_a_id,
        })
        if resp.status_code != 422:
            problems.append(f"swap (foreign opponent_player_id) expected 422, got {resp.status_code} {resp.text}")
        else:
            print(f"swap (foreign opponent_player_id) -> 422 ({resp.json().get('detail')})")

        # 2b. player_1_id (acting-for-another-player, admin path) belongs to
        #     the other club -> 422 via scoped x_signup lookup
        resp = client.post("/signups/swap", json={
            "system": TEST_SYSTEM, "week": WEEK_SWAP,
            "opponent_player_id": m_d, "player_1_id": other_player_a_id,
        })
        if resp.status_code != 422:
            problems.append(f"swap (foreign player_1_id) expected 422, got {resp.status_code} {resp.text}")
        else:
            print(f"swap (foreign player_1_id) -> 422 ({resp.json().get('detail')})")

        # 2c. same-club swap, self-serve path (caller is X themselves, m_a).
        #     Real swap: X<->Y, expect BYE pairings created for displaced Z and W.
        resp = client.post("/signups/swap", json={
            "system": TEST_SYSTEM, "week": WEEK_SWAP,
            "opponent_player_id": m_d,
        })
        if resp.status_code != 200:
            problems.append(f"swap (self-serve, same-club) expected 200, got {resp.status_code} {resp.text}")
        else:
            print(f"swap (self-serve, same-club) -> 200: {resp.json()['new_pairing']}, displaced={resp.json()['displaced']}")
        with Session(engine) as db:
            week_pairings = db.exec(select(Pairing).where(Pairing.week == WEEK_SWAP)).all()
            for p in week_pairings:
                if p.id not in created_pairing_ids:
                    created_pairing_ids.append(p.id)
                if p.club_id != manchester_id:
                    problems.append(f"swap created pairing id={p.id} with club_id={p.club_id}, expected {manchester_id}")
            xy_present = any(
                {p.a_signup_id, p.b_signup_id} == {x_signup_id, y_signup_id} for p in week_pairings
            )
            byes = {p.a_signup_id for p in week_pairings if p.b_signup_id is None}
            if not xy_present:
                problems.append("swap (self-serve) did not create the new X-Y prearranged pairing")
            else:
                print("swap (self-serve) created the new X-Y prearranged pairing")
            if z_signup_id not in byes or w_signup_id not in byes:
                problems.append(f"swap (self-serve) missing BYE pairing(s) for displaced Z/W: byes={byes}")
            else:
                print(f"swap (self-serve) created BYE pairings for displaced Z(m_b) and W(m_c), all club_id={manchester_id} verified")

        # 2d. admin-acting-for-another-player path, same club. Fresh week,
        #     fresh setup: admin (m_a) acts as X=m_b via player_1_id, swapping
        #     with Y=m_d (Y has no existing pairing this week, so no W-side BYE).
        with Session(engine) as db:
            x2_signup_id, z2_signup_id, xz2_pairing_id = _signup_pair(db, WEEK_SWAP_ADMIN, m_b, "X2", m_c, "Z2")
            y2_signup = Signup(week=WEEK_SWAP_ADMIN, system=TEST_SYSTEM, player_id=m_d, player_name="Y2", club_id=manchester_id)
            db.add(y2_signup)
            db.commit()
            db.refresh(y2_signup)
            y2_signup_id = y2_signup.id
            created_signup_ids.extend([x2_signup_id, z2_signup_id, y2_signup_id])
            created_pairing_ids.append(xz2_pairing_id)

            gate2 = PublishState(week=WEEK_SWAP_ADMIN, system=TEST_SYSTEM, published=True, club_id=manchester_id)
            db.add(gate2)
            db.commit()
            db.refresh(gate2)
            created_publish_ids.append(gate2.id)
        print(f"Set up admin-swap scenario: X2(m_b)={x2_signup_id} vs Z2(m_c)={z2_signup_id}, Y2(m_d)={y2_signup_id} unpaired, gate published")

        resp = client.post("/signups/swap", json={
            "system": TEST_SYSTEM, "week": WEEK_SWAP_ADMIN,
            "opponent_player_id": m_d, "player_1_id": m_b,
        })
        if resp.status_code != 200:
            problems.append(f"swap (admin-acting-for-another, same-club) expected 200, got {resp.status_code} {resp.text}")
        else:
            print(f"swap (admin-acting-for-another, same-club) -> 200: {resp.json()['new_pairing']}, displaced={resp.json()['displaced']}")
        with Session(engine) as db:
            week_pairings = db.exec(select(Pairing).where(Pairing.week == WEEK_SWAP_ADMIN)).all()
            for p in week_pairings:
                if p.id not in created_pairing_ids:
                    created_pairing_ids.append(p.id)
                if p.club_id != manchester_id:
                    problems.append(f"swap (admin) created pairing id={p.id} with club_id={p.club_id}, expected {manchester_id}")
            x2y2_present = any(
                {p.a_signup_id, p.b_signup_id} == {x2_signup_id, y2_signup_id} for p in week_pairings
            )
            byes2 = {p.a_signup_id for p in week_pairings if p.b_signup_id is None}
            if not x2y2_present:
                problems.append("swap (admin-acting-for-another) did not create the new X2-Y2 prearranged pairing")
            else:
                print("swap (admin-acting-for-another) created the new X2-Y2 prearranged pairing")
            if z2_signup_id not in byes2:
                problems.append(f"swap (admin-acting-for-another) missing BYE pairing for displaced Z2: byes={byes2}")
            else:
                print(f"swap (admin-acting-for-another) created BYE pairing for displaced Z2(m_c), club_id={manchester_id} verified")

        # =====================================================================
        # 3. drop_signup — pre-publish and post-publish branches, cross-club
        #    data present but untouched
        # =====================================================================
        # --- Pre-publish branch ---
        with Session(engine) as db:
            other_pre_signup = Signup(week=WEEK_DROP_PRE, system=TEST_SYSTEM, player_id=other_player_a_id, player_name="Other", club_id=other_club_id)
            db.add(other_pre_signup)
            db.commit()
            db.refresh(other_pre_signup)
            other_pre_signup_id = other_pre_signup.id
            created_signup_ids.append(other_pre_signup_id)
        # Manchester caller (m_a) signs up for the same week/system via the real endpoint
        resp = client.post("/signups", json={"system": TEST_SYSTEM, "week": WEEK_DROP_PRE, "faction": "Empire"})
        if resp.status_code != 200:
            problems.append(f"setup: POST /signups for drop pre-publish test failed: {resp.status_code} {resp.text}")
        m_pre_signup_id = resp.json()["signup"]["id"]
        created_signup_ids.append(m_pre_signup_id)

        resp = client.delete("/signups/mine", params={"system": TEST_SYSTEM, "week": WEEK_DROP_PRE})
        if resp.status_code != 200 or not resp.json().get("dropped"):
            problems.append(f"drop_signup (pre-publish) failed: {resp.status_code} {resp.text}")
        else:
            print(f"drop_signup (pre-publish) -> dropped=True")
        with Session(engine) as db:
            m_row = db.get(Signup, m_pre_signup_id)
            other_row = db.get(Signup, other_pre_signup_id)
            if m_row is not None:
                problems.append(f"drop_signup (pre-publish) did not delete Manchester's own signup id={m_pre_signup_id}")
            else:
                print(f"drop_signup (pre-publish) deleted Manchester's own signup id={m_pre_signup_id}")
            if other_row is None:
                problems.append("drop_signup (pre-publish) deleted the OTHER club's signup — cross-club leak!")
            else:
                print(f"drop_signup (pre-publish) left other club's signup id={other_pre_signup_id} untouched")

        # --- Post-publish branch ---
        with Session(engine) as db:
            m_post_signup = Signup(week=WEEK_DROP_POST, system=TEST_SYSTEM, player_id=m_a, player_name="X", club_id=manchester_id)
            opp_post_signup = Signup(week=WEEK_DROP_POST, system=TEST_SYSTEM, player_id=m_b, player_name="Opp", club_id=manchester_id)
            other_post_signup = Signup(week=WEEK_DROP_POST, system=TEST_SYSTEM, player_id=other_player_a_id, player_name="OtherClubPlayer", club_id=other_club_id)
            db.add(m_post_signup)
            db.add(opp_post_signup)
            db.add(other_post_signup)
            db.commit()
            db.refresh(m_post_signup)
            db.refresh(opp_post_signup)
            db.refresh(other_post_signup)
            m_post_signup_id = m_post_signup.id
            opp_post_signup_id = opp_post_signup.id
            other_post_signup_id = other_post_signup.id
            created_signup_ids.extend([m_post_signup_id, opp_post_signup_id, other_post_signup_id])

            m_post_pairing = Pairing(
                week=WEEK_DROP_POST, system=TEST_SYSTEM,
                a_signup_id=m_post_signup_id, b_signup_id=opp_post_signup_id,
                status="pending", prearranged=False, a_faction=None, b_faction=None,
                club_id=manchester_id,
            )
            other_post_pairing = Pairing(
                week=WEEK_DROP_POST, system=TEST_SYSTEM,
                a_signup_id=other_post_signup_id, b_signup_id=None,
                status="pending", prearranged=False, a_faction=None, b_faction=None,
                club_id=other_club_id,
            )
            db.add(m_post_pairing)
            db.add(other_post_pairing)
            db.commit()
            db.refresh(m_post_pairing)
            db.refresh(other_post_pairing)
            m_post_pairing_id = m_post_pairing.id
            other_post_pairing_id = other_post_pairing.id
            created_pairing_ids.extend([m_post_pairing_id, other_post_pairing_id])

            m_gate = PublishState(week=WEEK_DROP_POST, system=TEST_SYSTEM, published=True, club_id=manchester_id)
            other_gate = PublishState(week=WEEK_DROP_POST, system=TEST_SYSTEM, published=True, club_id=other_club_id)
            db.add(m_gate)
            db.add(other_gate)
            db.commit()
            db.refresh(m_gate)
            db.refresh(other_gate)
            created_publish_ids.extend([m_gate.id, other_gate.id])
        print(f"Set up post-publish drop scenario: m_pairing={m_post_pairing_id}, other_pairing={other_post_pairing_id}, both gates published")

        resp = client.delete("/signups/mine", params={"system": TEST_SYSTEM, "week": WEEK_DROP_POST})
        if resp.status_code != 200 or not resp.json().get("dropped") or not resp.json().get("published"):
            problems.append(f"drop_signup (post-publish) failed: {resp.status_code} {resp.text}")
        else:
            print(f"drop_signup (post-publish) -> dropped=True, published=True")

        with Session(engine) as db:
            m_row = db.get(Signup, m_post_signup_id)
            m_pairing_row = db.get(Pairing, m_post_pairing_id)
            other_signup_row = db.get(Signup, other_post_signup_id)
            other_pairing_row = db.get(Pairing, other_post_pairing_id)
            bye_pairings = db.exec(
                select(Pairing).where(Pairing.week == WEEK_DROP_POST).where(Pairing.club_id == manchester_id).where(Pairing.b_signup_id.is_(None))
            ).all()
            for p in bye_pairings:
                if p.id not in created_pairing_ids:
                    created_pairing_ids.append(p.id)

            if m_row is not None:
                problems.append("drop_signup (post-publish) did not delete Manchester's own signup")
            else:
                print("drop_signup (post-publish) deleted Manchester's own signup")
            if m_pairing_row is not None:
                problems.append("drop_signup (post-publish) did not delete Manchester's own pairing")
            else:
                print("drop_signup (post-publish) deleted Manchester's own pairing")
            if other_signup_row is None:
                problems.append("drop_signup (post-publish) deleted the OTHER club's signup — cross-club leak!")
            else:
                print("drop_signup (post-publish) left other club's signup untouched")
            if other_pairing_row is None:
                problems.append("drop_signup (post-publish) deleted the OTHER club's pairing — cross-club leak!")
            else:
                print("drop_signup (post-publish) left other club's pairing untouched")
            if not any(p.a_signup_id == opp_post_signup_id for p in bye_pairings):
                problems.append("drop_signup (post-publish) did not create a BYE pairing for the displaced Manchester opponent")
            else:
                print(f"drop_signup (post-publish) created a BYE pairing for the displaced Manchester opponent, club_id={manchester_id}")

        # =====================================================================
        # 4. _get_all_byes — club_id scoping
        # =====================================================================
        with Session(engine) as db:
            m_bye_signup = Signup(week=WEEK_BYES, system=TEST_SYSTEM, player_id=m_a, player_name="ManchesterBye", club_id=manchester_id)
            other_bye_signup = Signup(week=WEEK_BYES, system=TEST_SYSTEM, player_id=other_player_a_id, player_name="OtherBye", club_id=other_club_id)
            db.add(m_bye_signup)
            db.add(other_bye_signup)
            db.commit()
            db.refresh(m_bye_signup)
            db.refresh(other_bye_signup)
            created_signup_ids.extend([m_bye_signup.id, other_bye_signup.id])

            m_bye_pairing = Pairing(week=WEEK_BYES, system=TEST_SYSTEM, a_signup_id=m_bye_signup.id, b_signup_id=None, status="pending", prearranged=False, a_faction=None, b_faction=None, club_id=manchester_id)
            other_bye_pairing = Pairing(week=WEEK_BYES, system=TEST_SYSTEM, a_signup_id=other_bye_signup.id, b_signup_id=None, status="pending", prearranged=False, a_faction=None, b_faction=None, club_id=other_club_id)
            db.add(m_bye_pairing)
            db.add(other_bye_pairing)
            db.commit()
            db.refresh(m_bye_pairing)
            db.refresh(other_bye_pairing)
            created_pairing_ids.extend([m_bye_pairing.id, other_bye_pairing.id])

            m_gate = PublishState(week=WEEK_BYES, system=TEST_SYSTEM, published=True, club_id=manchester_id)
            other_gate = PublishState(week=WEEK_BYES, system=TEST_SYSTEM, published=True, club_id=other_club_id)
            db.add(m_gate)
            db.add(other_gate)
            db.commit()
            db.refresh(m_gate)
            db.refresh(other_gate)
            created_publish_ids.extend([m_gate.id, other_gate.id])

            manchester_byes = _get_all_byes(db, TEST_SYSTEM, WEEK_BYES, manchester_id)
            other_byes = _get_all_byes(db, TEST_SYSTEM, WEEK_BYES, other_club_id)

        m_names = {b["player_name"] for b in manchester_byes}
        other_names = {b["player_name"] for b in other_byes}
        if "OtherBye" in m_names or "ManchesterBye" not in m_names:
            problems.append(f"_get_all_byes(manchester_id) returned wrong set: {m_names}")
        else:
            print(f"_get_all_byes(manchester_id) -> {m_names} (correct, other club's BYE excluded)")
        if "ManchesterBye" in other_names or "OtherBye" not in other_names:
            problems.append(f"_get_all_byes(other_club_id) returned wrong set: {other_names}")
        else:
            print(f"_get_all_byes(other_club_id) -> {other_names} (correct, isolated the other way too)")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
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
            for pubid in created_publish_ids:
                pub = db.get(PublishState, pubid)
                if pub:
                    db.delete(pub)
            db.commit()
            for pid in created_player_ids:
                p = db.get(Player, pid)
                if p:
                    db.delete(p)
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
            "\nVerification passed: submit_prearranged() 404s with zero rows "
            "created when either player belongs to another club and works "
            "correctly same-club; swap_signups() 422s for a foreign "
            "player_1_id/opponent_player_id via the scoped signup lookup and "
            "works correctly same-club (both self-serve and admin-acting-for-"
            "another-player paths); drop_signup() only touches the caller's "
            "own club's rows in both the pre- and post-publish branches; "
            "_get_all_byes() is scoped by club_id."
        )


if __name__ == "__main__":
    main()
