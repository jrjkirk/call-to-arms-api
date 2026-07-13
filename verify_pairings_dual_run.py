"""One-off staging verification for the pairings_engine.py Phase 0 dual-run
refactor. Not a permanent test suite — see PROJECT_STATUS.md.

Staging has zero real Signup/Pairing/Player rows for any of the three
systems (checked directly before writing this script — the club's real
signup history isn't in this DB yet). So this builds synthetic signups
that exercise all six refactored branches (has_intro_prepass,
escalation_priority, uses_scenarios, uses_points, recent_weeks/
extended_weeks) for TOW/HH/KT, plus one historical pairing per system
(4 weeks back) to exercise the recent/extended rematch-penalty window.

It runs generate(persist=False) once with systems_from_catalogue off and
once with it on, against the identical underlying data, and diffs the
resulting pairing lists. Everything — synthetic signups/pairings and the
flag toggle — happens inside one transaction that is rolled back at the
end; nothing is committed to staging.

Run:
    python verify_pairings_dual_run.py
"""
import sys

from sqlmodel import Session

from database import engine
from models import AppSetting, Pairing, Signup
from pairings_engine import generate

TOW = "The Old World"
HH = "The Horus Heresy"
KT = "Kill Team"

CURRENT_WEEK = "27/07/2026"
PAST_WEEK = "29/06/2026"  # exactly 4 weeks before CURRENT_WEEK


def _mk_signup(week, system, player_id, name, **kwargs):
    return Signup(week=week, system=system, player_id=player_id, player_name=name, **kwargs)


def build_current_signups():
    rows = [
        # --- TOW: intro pre-pass, escalation priority, scenario diff, points distance ---
        _mk_signup(CURRENT_WEEK, TOW, 910001, "ZZTEST Alice Anderson", vibe="Casual", points=2000, scenario="Open Battle", eta="18:00"),
        _mk_signup(CURRENT_WEEK, TOW, 910002, "ZZTEST Bob Baker", vibe="Casual", points=2000, scenario="Open Battle", can_demo=True, eta="18:00"),
        _mk_signup(CURRENT_WEEK, TOW, 910003, "ZZTEST Carol Clark", vibe="Escalation", points=2500, scenario="Weekly Scenario", eta="18:30"),
        _mk_signup(CURRENT_WEEK, TOW, 910004, "ZZTEST Dave Dawson", vibe="Escalation", points=2500, scenario="Weekly Scenario", eta="18:30"),
        _mk_signup(CURRENT_WEEK, TOW, 910005, "ZZTEST Eve Evans", vibe="Intro", points=1000, eta="19:00"),
        _mk_signup(CURRENT_WEEK, TOW, 910006, "ZZTEST Frank Fisher", vibe="Competitive", points=2000, scenario="Open Battle", can_demo=True, eta="19:00"),
        _mk_signup(CURRENT_WEEK, TOW, 910007, "ZZTEST Grace Green", vibe="Either", points=1750, standby_ok=True),
        _mk_signup(CURRENT_WEEK, TOW, 910008, "ZZTEST Hank Harris", vibe="Casual", points=1750),
        _mk_signup(CURRENT_WEEK, TOW, 910009, "ZZTEST Ivy Irwin", vibe="Casual", points=2000, scenario="Open Battle", eta="18:00"),
        _mk_signup(CURRENT_WEEK, TOW, 910010, "ZZTEST Jack Jones", vibe="Casual", points=2000, scenario="Open Battle", eta="18:00"),

        # --- HH: intro pre-pass, no scenarios/escalation, wider recent/extended window ---
        _mk_signup(CURRENT_WEEK, HH, 920001, "ZZTEST Karl King", vibe="Standard", points=3000, eta="18:00"),
        _mk_signup(CURRENT_WEEK, HH, 920002, "ZZTEST Laura Lane", vibe="Standard", points=3000, eta="18:00"),
        _mk_signup(CURRENT_WEEK, HH, 920003, "ZZTEST Mike Moore", vibe="Intro", points=3000, eta="18:30"),
        _mk_signup(CURRENT_WEEK, HH, 920004, "ZZTEST Nina Noble", vibe="Standard", points=3000, can_demo=True, eta="18:30"),
        _mk_signup(CURRENT_WEEK, HH, 920005, "ZZTEST Oscar Owen", vibe="Standard", points=4000),
        _mk_signup(CURRENT_WEEK, HH, 920006, "ZZTEST Peggy Poole", vibe="Standard", points=4000),
        _mk_signup(CURRENT_WEEK, HH, 920007, "ZZTEST Quinn Quill", vibe="Standard", points=3000, eta="19:00"),
        _mk_signup(CURRENT_WEEK, HH, 920008, "ZZTEST Randy Ross", vibe="Standard", points=3000, eta="19:00"),
    ]

    # --- KT: no intro/escalation/scenarios; points must NOT affect matching ---
    kt_names = ["Sam Stone", "Tina Torres", "Uma Underwood", "Vince Vance",
                "Wendy Watts", "Xavier Xu", "Yara Young", "Zack Zane"]
    kt_points = [0, 500, 1000, 1500, 2000, 2500, 3000, 3500]
    for i, (name, pts) in enumerate(zip(kt_names, kt_points)):
        rows.append(_mk_signup(CURRENT_WEEK, KT, 930001 + i, f"ZZTEST {name}", vibe="Standard", points=pts))

    return rows


def build_history_signups():
    """Past-week signups for the rematch pair in TOW and HH."""
    return [
        _mk_signup(PAST_WEEK, TOW, 910009, "ZZTEST Ivy Irwin", vibe="Casual", points=2000, scenario="Open Battle"),
        _mk_signup(PAST_WEEK, TOW, 910010, "ZZTEST Jack Jones", vibe="Casual", points=2000, scenario="Open Battle"),
        _mk_signup(PAST_WEEK, HH, 920007, "ZZTEST Quinn Quill", vibe="Standard", points=3000),
        _mk_signup(PAST_WEEK, HH, 920008, "ZZTEST Randy Ross", vibe="Standard", points=3000),
    ]


def to_comparable(rows):
    return [{k: r[k] for k in sorted(r)} for r in rows]


def main():
    with Session(engine) as session:
        history_signups = build_history_signups()
        session.add_all(history_signups)
        session.flush()

        session.add(Pairing(
            week=PAST_WEEK, system=TOW,
            a_signup_id=history_signups[0].id, b_signup_id=history_signups[1].id,
            status="pending",
        ))
        session.add(Pairing(
            week=PAST_WEEK, system=HH,
            a_signup_id=history_signups[2].id, b_signup_id=history_signups[3].id,
            status="pending",
        ))
        session.flush()

        current_signups = build_current_signups()
        session.add_all(current_signups)
        session.flush()

        # Baseline: flag off (force it, in case a prior session left it on)
        flag_row = session.get(AppSetting, "systems_from_catalogue")
        if flag_row is None:
            flag_row = AppSetting(key="systems_from_catalogue", value="false")
        else:
            flag_row.value = "false"
        session.add(flag_row)
        session.flush()

        baseline = {
            system: to_comparable(generate(session, CURRENT_WEEK, system, persist=False))
            for system in (TOW, HH, KT)
        }

        # Catalogue-driven: flip flag on, same underlying data
        flag_row.value = "true"
        session.add(flag_row)
        session.flush()

        catalogue = {
            system: to_comparable(generate(session, CURRENT_WEEK, system, persist=False))
            for system in (TOW, HH, KT)
        }

        session.rollback()  # discard synthetic signups/pairing/flag — nothing hits staging

    mismatches = []
    for system in (TOW, HH, KT):
        if baseline[system] != catalogue[system]:
            mismatches.append(system)
            print(f"MISMATCH for {system}:")
            print("  baseline :", baseline[system])
            print("  catalogue:", catalogue[system])
        else:
            print(f"{system}: MATCH ({len(baseline[system])} pairing rows, byte-identical)")

    if mismatches:
        print(f"\nFAILED: {len(mismatches)} system(s) mismatched: {mismatches}")
        sys.exit(1)

    print("\nAll three systems: catalogue-driven output == hardcoded output, byte-identical.")


if __name__ == "__main__":
    main()
