"""Pairing generation engine — faithful port of the original Streamlit matcher.

9-tuple _pair_dist order: (block_pen, esc_p, mir, rematch_p, dv, de, eta_b, scen_d, dp)
Do NOT reorder or "optimise" — the tuple order encodes matchmaking priority.
T&T / 3-way grouping intentionally removed (club never uses it).
Odd numbers naturally produce a single BYE via the greedy fallback.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from models import Pairing, PairingBlock, Signup


def _normalize_name(n: str) -> str:
    return " ".join(n.strip().split())


def build_match_preference(su: Signup) -> tuple:
    v = (su.vibe or "").strip().lower()
    vibe_w = 0 if (v.startswith("casual") or v == "escalation") else 1

    exp_map = {"new": 0, "some": 1, "veteran": 2, "experienced": 2}
    exp_w = 1
    exp_str = (su.experience or "").lower()
    for k, val in exp_map.items():
        if k in exp_str:
            exp_w = val
            break

    pts_bucket = int(round((su.points or 0) / 250.0))
    return (vibe_w, exp_w, pts_bucket)


def previous_pairs_recent(
    session: Session, system: str, current_week: str, max_weeks: int
) -> set:
    try:
        current_dt = datetime.strptime(current_week, "%d/%m/%Y")
    except ValueError:
        return set()

    pairings = session.exec(
        select(Pairing)
        .where(Pairing.system == system)
        .where(Pairing.b_signup_id.isnot(None))
    ).all()

    if not pairings:
        return set()

    signup_ids = {p.a_signup_id for p in pairings} | {
        p.b_signup_id for p in pairings if p.b_signup_id
    }
    rows = session.exec(select(Signup).where(Signup.id.in_(signup_ids))).all()
    signups_by_id = {s.id: s for s in rows}

    result: set = set()
    for pr in pairings:
        try:
            pr_week_dt = datetime.strptime(pr.week, "%d/%m/%Y")
        except ValueError:
            continue
        if abs((current_dt - pr_week_dt).days) // 7 > max_weeks:
            continue
        a_su = signups_by_id.get(pr.a_signup_id)
        b_su = signups_by_id.get(pr.b_signup_id) if pr.b_signup_id else None
        if not a_su or not b_su:
            continue
        a_name = _normalize_name(a_su.player_name).lower()
        b_name = _normalize_name(b_su.player_name).lower()
        if a_name == b_name:
            continue
        result.add(tuple(sorted([a_name, b_name])))
    return result


@dataclass
class MatcherSignup:
    row: Signup
    key: str
    preference: tuple


# ---------------------------------------------------------------------------
# Distance sub-helpers
# ---------------------------------------------------------------------------

def _eta_minutes(eta: Optional[str]) -> Optional[int]:
    if not eta:
        return None
    try:
        h, m = eta.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _eta_bucket_diff(a: MatcherSignup, b: MatcherSignup) -> int:
    am = _eta_minutes(a.row.eta)
    bm = _eta_minutes(b.row.eta)
    if am is None or bm is None:
        return 1
    diff = abs(am - bm)
    if diff <= 30:
        return 0
    if diff <= 60:
        return 1
    return 2


def _scenario_diff_tow(a: MatcherSignup, b: MatcherSignup, system: str) -> int:
    if system != "The Old World":
        return 0
    a_sc = (a.row.scenario or "").strip()
    b_sc = (b.row.scenario or "").strip()
    if not a_sc or not b_sc:
        return 1
    return 0 if a_sc.lower() == b_sc.lower() else 1


def _mirror_flag(a: MatcherSignup, b: MatcherSignup) -> int:
    af = (a.row.faction or "").lower().strip()
    bf = (b.row.faction or "").lower().strip()
    return 1 if (af and bf and af == bf) else 0


def _vibe_distance_override(a: MatcherSignup, b: MatcherSignup, base: int) -> int:
    av = (a.row.vibe or "").lower().strip()
    bv = (b.row.vibe or "").lower().strip()
    if av == "intro" or bv == "intro":
        return base
    if av == "either" or bv == "either":
        return 0
    return 0 if av == bv else 1


def _escalation_priority_penalty(a: MatcherSignup, b: MatcherSignup, system: str) -> int:
    if system != "The Old World":
        return 0
    av = (a.row.vibe or "").lower().strip()
    bv = (b.row.vibe or "").lower().strip()
    if av != "escalation":
        return 0
    if bv == "escalation":
        return 0
    if bv in ("casual", "either"):
        return 1
    return 2


def _pair_dist(
    ms: MatcherSignup,
    other: MatcherSignup,
    system: str,
    seen_recent: set,
    seen_extended: set,
    blocks: set,
) -> tuple:
    a_pid = ms.row.player_id
    b_pid = other.row.player_id
    block_pen = (
        1
        if (
            a_pid is not None
            and b_pid is not None
            and tuple(sorted([a_pid, b_pid])) in blocks
        )
        else 0
    )
    esc_p = _escalation_priority_penalty(ms, other, system)
    mir = _mirror_flag(ms, other)

    pair_key = tuple(sorted([ms.key, other.key]))
    if pair_key in seen_recent:
        rematch_p = 2
    elif pair_key in seen_extended:
        rematch_p = 1
    else:
        rematch_p = 0

    dv = _vibe_distance_override(ms, other, abs(ms.preference[0] - other.preference[0]))
    de = abs(ms.preference[1] - other.preference[1])
    eta_b = _eta_bucket_diff(ms, other)
    scen_d = _scenario_diff_tow(ms, other, system)
    dp = 0 if system == "Kill Team" else abs(ms.preference[2] - other.preference[2])

    return (block_pen, esc_p, mir, rematch_p, dv, de, eta_b, scen_d, dp)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate(
    session: Session,
    week: str,
    system: str,
    allow_repeats_when_needed: bool = True,
    *,
    persist: bool,
) -> list:
    """Generate pairings.

    persist=True  → writes Pairing rows, commits, returns list[Pairing].
    persist=False → dry run, returns list[dict] with no DB writes.
    """
    # 1. Prearranged signup ids — excluded from matching pool
    prearranged_rows = session.exec(
        select(Pairing)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .where(Pairing.prearranged == True)
    ).all()
    prearranged_ids: set[int] = set()
    for pr in prearranged_rows:
        prearranged_ids.add(pr.a_signup_id)
        if pr.b_signup_id:
            prearranged_ids.add(pr.b_signup_id)

    # 2. Load signups, filter prearranged, de-dupe to latest-by-normalized-lower-name
    all_signups = session.exec(
        select(Signup)
        .where(Signup.week == week)
        .where(Signup.system == system)
        .order_by(Signup.created_at)
    ).all()

    filtered = [s for s in all_signups if s.id not in prearranged_ids]

    seen_names: dict[str, Signup] = {}
    for su in filtered:
        key = _normalize_name(su.player_name).lower()
        seen_names[key] = su  # later entry = higher created_at (ascending order)

    if not seen_names:
        return []

    candidates: list[MatcherSignup] = [
        MatcherSignup(row=su, key=k, preference=build_match_preference(su))
        for k, su in seen_names.items()
    ]

    # 3. Intro pre-pass (TOW and HH only, never KT)
    intro_pairs: list = []
    if system in ("The Old World", "The Horus Heresy"):
        used_keys: set[str] = set()
        seekers = [ms for ms in candidates if (ms.row.vibe or "").lower() == "intro"]
        leaders = [ms for ms in candidates if ms.row.can_demo]

        for seeker in seekers:
            if seeker.key in used_keys:
                continue
            best_leader: Optional[MatcherSignup] = None
            best_diff: Optional[tuple] = None
            for leader in leaders:
                if leader.key == seeker.key or leader.key in used_keys:
                    continue
                diff = (
                    abs(seeker.preference[0] - leader.preference[0]),
                    abs(seeker.preference[1] - leader.preference[1]),
                    abs(seeker.preference[2] - leader.preference[2]),
                )
                if best_diff is None or diff < best_diff:
                    best_leader = leader
                    best_diff = diff
                    if diff == (0, 0, 0):
                        break
            if best_leader is not None:
                used_keys.add(seeker.key)
                used_keys.add(best_leader.key)
                if persist:
                    p = Pairing(
                        week=week, system=system,
                        a_signup_id=seeker.row.id,
                        b_signup_id=best_leader.row.id,
                        status="pending",
                        a_faction=seeker.row.faction,
                        b_faction=best_leader.row.faction,
                    )
                    session.add(p)
                    session.flush()
                    intro_pairs.append(p)
                else:
                    intro_pairs.append({
                        "a_signup_id": seeker.row.id,
                        "a_name": seeker.row.player_name,
                        "a_faction": seeker.row.faction,
                        "b_signup_id": best_leader.row.id,
                        "b_name": best_leader.row.player_name,
                        "b_faction": best_leader.row.faction,
                    })

        candidates = [ms for ms in candidates if ms.key not in used_keys]

    # 4. Sort remaining candidates
    candidates.sort(
        key=lambda ms: (
            (0 if (ms.row.vibe or "").lower() == "escalation" else 1)
            if system == "The Old World"
            else 0,
            ms.preference,
            ms.key,
        )
    )

    # 5. Recent / extended play history
    if system == "The Horus Heresy":
        recent_w, extended_w = 6, 12
    else:
        recent_w, extended_w = 3, 6

    seen_recent = previous_pairs_recent(session, system, week, recent_w)
    seen_extended = previous_pairs_recent(session, system, week, extended_w)

    def has_played(x: MatcherSignup, y: MatcherSignup) -> bool:
        return tuple(sorted([x.key, y.key])) in seen_recent

    # 6. Load blocks
    block_rows = session.exec(select(PairingBlock)).all()
    blocks: set = {tuple(sorted([b.player_a_id, b.player_b_id])) for b in block_rows}

    # 7. Greedy matching; out starts with intro pairs
    used: set[str] = set()
    out: list = list(intro_pairs)

    for i, ms in enumerate(candidates):
        if ms.key in used:
            continue

        best_j: Optional[int] = None
        best_dist: Optional[tuple] = None

        # First pass: skip recent repeats
        for j in range(i + 1, len(candidates)):
            other = candidates[j]
            if other.key in used:
                continue
            if has_played(ms, other):
                continue
            d = _pair_dist(ms, other, system, seen_recent, seen_extended, blocks)
            if best_dist is None or d < best_dist:
                best_dist = d
                best_j = j
                if d[:7] == (0, 0, 0, 0, 0, 0, 0):
                    break

        # Second pass: allow recent repeats if needed
        if best_j is None and allow_repeats_when_needed:
            for j in range(i + 1, len(candidates)):
                other = candidates[j]
                if other.key in used:
                    continue
                d = _pair_dist(ms, other, system, seen_recent, seen_extended, blocks)
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best_j = j
                    if d[:7] == (0, 0, 0, 0, 0, 0, 0):
                        break

        if best_j is not None:
            other = candidates[best_j]
            used.add(ms.key)
            used.add(other.key)
            if persist:
                p = Pairing(
                    week=week, system=system,
                    a_signup_id=ms.row.id,
                    b_signup_id=other.row.id,
                    status="pending",
                    a_faction=ms.row.faction,
                    b_faction=other.row.faction,
                )
                session.add(p)
                session.flush()
                out.append(p)
            else:
                out.append({
                    "a_signup_id": ms.row.id,
                    "a_name": ms.row.player_name,
                    "a_faction": ms.row.faction,
                    "b_signup_id": other.row.id,
                    "b_name": other.row.player_name,
                    "b_faction": other.row.faction,
                })
        else:
            # BYE
            used.add(ms.key)
            if persist:
                p = Pairing(
                    week=week, system=system,
                    a_signup_id=ms.row.id,
                    b_signup_id=None,
                    status="pending",
                    a_faction=ms.row.faction,
                    b_faction=None,
                )
                session.add(p)
                session.flush()
                out.append(p)
            else:
                out.append({
                    "a_signup_id": ms.row.id,
                    "a_name": ms.row.player_name,
                    "a_faction": ms.row.faction,
                    "b_signup_id": None,
                    "b_name": None,
                    "b_faction": None,
                })

    if persist:
        session.commit()

    return out
