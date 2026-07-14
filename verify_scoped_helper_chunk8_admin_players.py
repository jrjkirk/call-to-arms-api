"""One-off verification script for the scoped-query-helper phase, chunk 8
(admin.py's remaining players/settings/signup-list endpoints -- the last
chunk of admin.py). Not part of the app; run manually against staging.

Proves, beyond "no errors":

  1. grantable_users: Manchester's GET /admin/grantable-users only ever
     returns Manchester's own users (with linked active players), with a
     second club's identically-shaped user+player present the whole time.
  2. admin_players: both branches (scope=... and no-scope super-admin
     branch) of GET /admin/players are isolated per club.
  3. patch_player ownership-check fix, proven via a direct row read (not
     just the status code): PATCH on another club's player id -> 404, row
     unmodified. Same-club patch still works (positive control).
  4. block_players: GET /admin/blocks/players isolated per club.
  5. auto-pairings-settings: POST/GET for the same system, two different
     clubs, produce independent club_settings rows (verified via direct
     row read) -- one club's write doesn't clobber or leak into the
     other's GET.
  6. pairings_signup_list: GET /admin/pairings/signup-list isolated per
     club for an identical week/system key.

All rows this script creates (a genuine second temp club + its
players/users/signups, temp Manchester players/users/signups, and any
club_settings rows) are cleaned up in a `finally`, leaving staging exactly
as it started.

Run with: python verify_scoped_helper_chunk8_admin_players.py
"""
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from database import engine
from models import Club, ClubSetting, Player, Signup, User
from main import app
import auth

TEST_SYSTEM = "Kill Team"
TEST_SLUG = "KillTeam"

WEEK_SIGNUP_LIST = "26/01/2099"


def _fake_user(club_id: int, uid: int) -> User:
    return User(
        id=uid,
        discord_id=f"test-verify-chunk8-{uid}",
        discord_name=f"Verify Script User {uid}",
        player_id=None,
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
    created_user_ids = []
    created_signup_ids = []
    created_setting_keys = []  # list of (club_id, key)

    def _mk_player(name, club_id):
        with Session(engine) as db:
            p = Player(name=name, active=True, club_id=club_id)
            db.add(p)
            db.commit()
            db.refresh(p)
            created_player_ids.append(p.id)
            return p.id

    def _mk_user_row(club_id, player_id, discord_suffix):
        with Session(engine) as db:
            u = User(
                discord_id=f"zztest-chunk8-{discord_suffix}",
                discord_name=f"ZZTest Chunk8 {discord_suffix}",
                player_id=player_id,
                is_super_admin=False,
                club_id=club_id,
            )
            db.add(u)
            db.commit()
            db.refresh(u)
            created_user_ids.append(u.id)
            return u

    def _mk_signup(club_id, week, player_id, player_name, **kw):
        with Session(engine) as db:
            defaults = dict(standby_ok=False, tnt_ok=False, can_demo=False)
            defaults.update(kw)
            su = Signup(
                week=week, system=TEST_SYSTEM, player_id=player_id,
                player_name=player_name, club_id=club_id, **defaults,
            )
            db.add(su)
            db.commit()
            db.refresh(su)
            created_signup_ids.append(su.id)
            return su

    def _get_player(pid):
        with Session(engine) as db:
            return db.get(Player, pid)

    def _get_setting_row(club_id, key):
        with Session(engine) as db:
            return db.get(ClubSetting, (club_id, key))

    try:
        # =====================================================================
        # 0. Second temp club
        # =====================================================================
        with Session(engine) as db:
            other_club = Club(name="ZZTest Chunk8 Other Club", slug="zztest-chunk8-other-club")
            db.add(other_club)
            db.commit()
            db.refresh(other_club)
            other_club_id = other_club.id
        print(f"Created other club id={other_club_id}")

        manchester_admin = _fake_user(manchester_id, uid=999401)
        other_admin = _fake_user(other_club_id, uid=999402)

        def _as(user):
            app.dependency_overrides[auth.require_user] = lambda: user
            app.dependency_overrides[auth.current_user] = lambda: user

        # =====================================================================
        # 1. grantable_users isolation.
        # =====================================================================
        m_gu_player = _mk_player("ZZTest Chunk8 M-Grantable", manchester_id)
        o_gu_player = _mk_player("ZZTest Chunk8 O-Grantable", other_club_id)
        m_gu_user = _mk_user_row(manchester_id, m_gu_player, "m-grantable")
        o_gu_user = _mk_user_row(other_club_id, o_gu_player, "o-grantable")

        _as(manchester_admin)
        r = client.get("/admin/grantable-users")
        if r.status_code != 200:
            problems.append(f"GET /admin/grantable-users (manchester) failed: {r.status_code} {r.text}")
        else:
            ids_seen = {row["id"] for row in r.json()}
            if o_gu_user.id in ids_seen:
                problems.append(f"grantable_users LEAK: manchester's list included the other club's user id {o_gu_user.id}")
            if m_gu_user.id not in ids_seen:
                problems.append(f"grantable_users: manchester's own user id {m_gu_user.id} missing from its own list")
            if o_gu_user.id not in ids_seen and m_gu_user.id in ids_seen:
                print("grantable_users: manchester sees only its own user, other club's excluded -- OK")

        # =====================================================================
        # 2. admin_players -- scope-provided branch.
        # =====================================================================
        m_ap_scoped = _mk_player("ZZTest Chunk8 M-Players-Scoped", manchester_id)
        o_ap_scoped = _mk_player("ZZTest Chunk8 O-Players-Scoped", other_club_id)

        _as(manchester_admin)
        r = client.get("/admin/players", params={"scope": TEST_SYSTEM})
        if r.status_code != 200:
            problems.append(f"GET /admin/players?scope=... (manchester) failed: {r.status_code} {r.text}")
        else:
            ids_seen = {row["id"] for row in r.json()}
            if o_ap_scoped in ids_seen:
                problems.append(f"admin_players(scope=...) LEAK: manchester's list included the other club's player id {o_ap_scoped}")
            if m_ap_scoped not in ids_seen:
                problems.append(f"admin_players(scope=...): manchester's own player id {m_ap_scoped} missing")
            if o_ap_scoped not in ids_seen and m_ap_scoped in ids_seen:
                print("admin_players(scope=...): manchester sees only its own players, other club's excluded -- OK")

        # =====================================================================
        # 3. admin_players -- no-scope super-admin branch.
        # =====================================================================
        r = client.get("/admin/players")
        if r.status_code != 200:
            problems.append(f"GET /admin/players (no scope, manchester) failed: {r.status_code} {r.text}")
        else:
            ids_seen = {row["id"] for row in r.json()}
            if o_ap_scoped in ids_seen:
                problems.append(f"admin_players(no scope) LEAK: manchester's full list included the other club's player id {o_ap_scoped}")
            if m_ap_scoped not in ids_seen:
                problems.append(f"admin_players(no scope): manchester's own player id {m_ap_scoped} missing")
            if o_ap_scoped not in ids_seen and m_ap_scoped in ids_seen:
                print("admin_players(no scope): manchester sees only its own players, other club's excluded -- OK")

        # =====================================================================
        # 4. patch_player ownership check.
        # =====================================================================
        m_patch_player = _mk_player("ZZTest Chunk8 M-Patch", manchester_id)
        o_patch_player = _mk_player("ZZTest Chunk8 O-Patch", other_club_id)

        _as(manchester_admin)
        r = client.patch(f"/admin/players/{o_patch_player}", json={"name": "HACKED"})
        if r.status_code != 404:
            problems.append(f"patch_player OWNERSHIP BUG: manchester caller patched other club's player, got status {r.status_code} (expected 404)")
        else:
            print("patch_player: PATCH on other club's player id correctly 404s -- OK")

        o_patch_after = _get_player(o_patch_player)
        if o_patch_after.name == "HACKED":
            problems.append("patch_player OWNERSHIP BUG: other club's player row was actually modified despite the 404")
        else:
            print("patch_player: other club's player row NOT modified (direct row read) -- OK")

        # Positive control: manchester patching its own player works.
        r = client.patch(f"/admin/players/{m_patch_player}", json={"name": "ZZTest Chunk8 M-Patch Renamed"})
        if r.status_code != 200:
            problems.append(f"patch_player: own-club patch failed (positive control): {r.status_code} {r.text}")
        else:
            m_patch_after = _get_player(m_patch_player)
            if m_patch_after.name != "ZZTest Chunk8 M-Patch Renamed":
                problems.append(f"patch_player: own-club row was NOT updated as expected (positive control failed): {m_patch_after.name}")
            else:
                print("patch_player: own-club row correctly updated (positive control) -- OK")

        # =====================================================================
        # 5. block_players isolation.
        # =====================================================================
        m_bp_player = _mk_player("ZZTest Chunk8 M-Blocks", manchester_id)
        o_bp_player = _mk_player("ZZTest Chunk8 O-Blocks", other_club_id)

        _as(manchester_admin)
        r = client.get("/admin/blocks/players")
        if r.status_code != 200:
            problems.append(f"GET /admin/blocks/players (manchester) failed: {r.status_code} {r.text}")
        else:
            ids_seen = {row["id"] for row in r.json()}
            if o_bp_player in ids_seen:
                problems.append(f"block_players LEAK: manchester's list included the other club's player id {o_bp_player}")
            if m_bp_player not in ids_seen:
                problems.append(f"block_players: manchester's own player id {m_bp_player} missing")
            if o_bp_player not in ids_seen and m_bp_player in ids_seen:
                print("block_players: manchester sees only its own players, other club's excluded -- OK")

        # =====================================================================
        # 6. auto-pairings-settings isolation (get + post, two clubs).
        # =====================================================================
        _as(manchester_admin)
        r = client.post("/admin/auto-pairings-settings", json={
            "system": TEST_SYSTEM, "enabled": True, "day": "Wednesday", "time": "19:00",
        })
        if r.status_code != 200:
            problems.append(f"POST /admin/auto-pairings-settings (manchester) failed: {r.status_code} {r.text}")
        else:
            print("auto-pairings-settings: manchester POST succeeded -- OK")
        for suffix in ("enabled", "day", "time"):
            created_setting_keys.append((manchester_id, f"auto_pairings_{TEST_SLUG}_{suffix}"))

        _as(other_admin)
        r = client.post("/admin/auto-pairings-settings", json={
            "system": TEST_SYSTEM, "enabled": False, "day": "Friday", "time": "21:00",
        })
        if r.status_code != 200:
            problems.append(f"POST /admin/auto-pairings-settings (other club) failed: {r.status_code} {r.text}")
        else:
            print("auto-pairings-settings: other club's POST succeeded -- OK")
        for suffix in ("enabled", "day", "time"):
            created_setting_keys.append((other_club_id, f"auto_pairings_{TEST_SLUG}_{suffix}"))

        _as(manchester_admin)
        r = client.get("/admin/auto-pairings-settings", params={"system": TEST_SYSTEM})
        if r.status_code != 200:
            problems.append(f"GET /admin/auto-pairings-settings (manchester) failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            if body["day"] != "Wednesday" or body["time"] != "19:00" or body["enabled"] is not True:
                problems.append(f"auto-pairings-settings LEAK/CLOBBER: manchester's GET returned {body}, expected day=Wednesday time=19:00 enabled=True")
            else:
                print("auto-pairings-settings: manchester's GET returns its own values, unaffected by other club's POST -- OK")

        _as(other_admin)
        r = client.get("/admin/auto-pairings-settings", params={"system": TEST_SYSTEM})
        if r.status_code != 200:
            problems.append(f"GET /admin/auto-pairings-settings (other club) failed: {r.status_code} {r.text}")
        else:
            body = r.json()
            if body["day"] != "Friday" or body["time"] != "21:00" or body["enabled"] is not False:
                problems.append(f"auto-pairings-settings LEAK/CLOBBER: other club's GET returned {body}, expected day=Friday time=21:00 enabled=False")
            else:
                print("auto-pairings-settings: other club's GET returns its own values, unaffected by manchester's POST -- OK")

        # Direct row-level confirmation: two independent rows per key, not one shared row.
        m_day_row = _get_setting_row(manchester_id, f"auto_pairings_{TEST_SLUG}_day")
        o_day_row = _get_setting_row(other_club_id, f"auto_pairings_{TEST_SLUG}_day")
        if m_day_row is None or o_day_row is None:
            problems.append("auto-pairings-settings: expected a club_settings row for both clubs, at least one missing")
        elif m_day_row.value != "Wednesday" or o_day_row.value != "Friday":
            problems.append(f"auto-pairings-settings: direct row read mismatch -- manchester={m_day_row.value}, other={o_day_row.value}")
        else:
            print("auto-pairings-settings: direct club_settings row read confirms two independent rows, correctly keyed by club_id -- OK")

        # =====================================================================
        # 7. pairings_signup_list isolation.
        # =====================================================================
        m_sl_player = _mk_player("ZZTest Chunk8 M-SignupList", manchester_id)
        o_sl_player = _mk_player("ZZTest Chunk8 O-SignupList", other_club_id)
        m_sl_signup = _mk_signup(manchester_id, WEEK_SIGNUP_LIST, m_sl_player, "ZZTest Chunk8 M-SignupList")
        o_sl_signup = _mk_signup(other_club_id, WEEK_SIGNUP_LIST, o_sl_player, "ZZTest Chunk8 O-SignupList")

        _as(manchester_admin)
        r = client.get("/admin/pairings/signup-list", params={"system": TEST_SYSTEM, "week": WEEK_SIGNUP_LIST})
        if r.status_code != 200:
            problems.append(f"GET /admin/pairings/signup-list (manchester) failed: {r.status_code} {r.text}")
        else:
            ids_seen = {row["id"] for row in r.json()}
            if o_sl_signup.id in ids_seen:
                problems.append(f"pairings_signup_list LEAK: manchester's list included the other club's signup id {o_sl_signup.id}")
            if m_sl_signup.id not in ids_seen:
                problems.append(f"pairings_signup_list: manchester's own signup id {m_sl_signup.id} missing")
            if o_sl_signup.id not in ids_seen and m_sl_signup.id in ids_seen:
                print("pairings_signup_list: manchester sees only its own signup, other club's excluded -- OK")

    finally:
        app.dependency_overrides.clear()
        with Session(engine) as db:
            for uid in created_user_ids:
                u = db.get(User, uid)
                if u:
                    db.delete(u)
            db.commit()
            for sid in created_signup_ids:
                s = db.get(Signup, sid)
                if s:
                    db.delete(s)
            db.commit()
            for pid in created_player_ids:
                p = db.get(Player, pid)
                if p:
                    db.delete(p)
            db.commit()
            for club_id, key in created_setting_keys:
                row = db.get(ClubSetting, (club_id, key))
                if row:
                    db.delete(row)
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
            "\nVerification passed: grantable_users, both admin_players branches, "
            "block_players, and pairings_signup_list are all correctly isolated per "
            "club; patch_player's new ownership check 404s for cross-club access "
            "(confirmed via direct row read) while same-club patch still works; and "
            "auto-pairings-settings reads/writes are correctly scoped per club, "
            "confirmed both via the API and a direct club_settings row read."
        )


if __name__ == "__main__":
    main()
