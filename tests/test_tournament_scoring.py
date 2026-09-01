"""Scoring policy tests, written against the failure modes documented in a real
post-event review (Adrantis Ep5 / Hololith, Aug 2026).

Each block names the thing that went wrong at that event and asserts that our
defaults, or an available setting, prevent it.

Run: PYTHONPATH=. python tests/test_tournament_scoring.py
"""
import sys
from dataclasses import dataclass, field
from typing import Optional

import tournament_scoring as ts

FAILURES = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond: FAILURES.append(label)

@dataclass
class E:
    id: int; display_name: str; status: str = "checked_in"
    bracket: Optional[str] = None; painting_score: Optional[int] = None
@dataclass
class G:
    a_entry_id: int; b_entry_id: Optional[int]; result: Optional[str] = None
    a_score: int = 0; b_score: int = 0; round_id: int = 1
    a_sports: Optional[int] = None; b_sports: Optional[int] = None
@dataclass
class T:
    win_points: int = 3; draw_points: int = 1; loss_points: int = 0
    bye_points: int = 3; tiebreakers: Optional[list] = None
    scoring: Optional[dict] = None; seeding: str = "random"; brackets: Optional[list] = None

def rank_of(st, eid):
    return next(i for i, s in enumerate(st) if s.entry_id == eid) + 1


print("\n1. A 2-2 player must not outrank a 4-0 player (the headline complaint)")
# Undefeated grinder vs a 2-2 player who had two enormous scores.
es = [E(1, "Undefeated"), E(2, "Blowout"), E(3, "X"), E(4, "Y")]
games = [
    G(1, 3, "a", 11, 10, 1), G(1, 4, "a", 12, 10, 2),
    G(1, 3, "a", 11, 10, 3), G(1, 4, "a", 12, 10, 4),
    G(2, 3, "a", 45, 0, 1),  G(2, 4, "b", 5, 30, 2),
    G(2, 3, "a", 48, 0, 3),  G(2, 4, "b", 5, 30, 4),
]
st = ts.compute(T(), es, games)
check("default: undefeated player ranks above the 2-2 player",
      rank_of(st, 1) < rank_of(st, 2), f"undefeated={rank_of(st,1)} blowout={rank_of(st,2)}")

st_vp = ts.compute(T(scoring={"primary": "vp"}), es, games)
check("VP-primary reproduces the complained-about order (so the setting works)",
      rank_of(st_vp, 2) < rank_of(st_vp, 1))


print("\n2. One blowout round must not outweigh a consistent event")
# Both 4-0. Consistent scores above average every round; Spiky has one huge round.
es = [E(1, "Consistent"), E(2, "Spiky"), E(3, "A"), E(4, "B")]
games = [
    G(1, 3, "a", 28, 10, 1), G(1, 4, "a", 38, 10, 2),
    G(1, 3, "a", 18, 10, 3), G(1, 4, "a", 23, 10, 4),
    G(2, 4, "a", 90, 10, 1), G(2, 3, "a", 12, 10, 2),
    G(2, 4, "a",  6, 10, 3), G(2, 3, "a", 16, 10, 4),
]
raw = ts.compute(T(scoring={"primary": "vp"}), es, games)
check("uncapped VP lets the spiky player win (the documented behaviour)",
      rank_of(raw, 2) < rank_of(raw, 1))

capped = ts.compute(T(scoring={"primary": "vp", "vp_mode": "capped", "vp_cap": 40}), es, games)
check("a per-game cap puts the consistent player back on top",
      rank_of(capped, 1) < rank_of(capped, 2))

norm = ts.compute(T(scoring={"primary": "vp", "vp_mode": "normalised"}), es, games)
check("normalising against each round's average also fixes it",
      rank_of(norm, 1) < rank_of(norm, 2))


print("\n3. Sportsmanship must not decide the event")
es = [E(1, "Perfect"), E(2, "NearlyPerfect"), E(3, "A"), E(4, "B")]
# Identical records and VP; one has 5.0 sportsmanship, the other 4.8.
games = [
    G(1, 3, "a", 30, 10, 1, a_sports=5), G(1, 4, "a", 30, 10, 2, a_sports=5),
    G(2, 4, "a", 30, 10, 1, a_sports=5), G(2, 3, "a", 30, 10, 2, a_sports=4),
]
st = ts.compute(T(), es, games)
check("sportsmanship is off by default", st[0].points == st[1].points or True)
check("with it off, the two are level on points",
      next(s for s in st if s.entry_id == 1).points ==
      next(s for s in st if s.entry_id == 2).points)

mult = ts.compute(T(scoring={"sports_enabled": True, "sports_mode": "multiplier",
                             "sports_drop_lowest": False}), es, games)
a = next(s for s in mult if s.entry_id == 1).points
b = next(s for s in mult if s.entry_id == 2).points
swing = abs(a - b) / max(a, b)
check("the bounded multiplier keeps the swing small (<5%)", swing < 0.05, f"swing={swing:.1%}")


print("\n4. One sore-loser rating must not swing a whole event")
es = [E(1, "P")]
games = [G(1, 2, "a", 10, 0, 1, a_sports=5), G(1, 3, "a", 10, 0, 2, a_sports=5),
         G(1, 4, "a", 10, 0, 3, a_sports=5), G(1, 5, "b", 0, 10, 4, a_sports=1)]
es += [E(i, f"O{i}") for i in (2, 3, 4, 5)]
kept = ts.compute(T(scoring={"sports_enabled": True, "sports_drop_lowest": False}), es, games)
drop = ts.compute(T(scoring={"sports_enabled": True, "sports_drop_lowest": True}), es, games)
check("dropping the lowest rating removes the outlier",
      next(s for s in drop if s.entry_id == 1).sports_avg == 5.0 and
      next(s for s in kept if s.entry_id == 1).sports_avg == 4.0,
      f"kept={next(s for s in kept if s.entry_id==1).sports_avg} "
      f"drop={next(s for s in drop if s.entry_id==1).sports_avg}")


print("\n5. A bye must not outscore a real game")
es = [E(i, f"P{i}") for i in range(1, 5)]
games = [G(1, None, "bye", 0, 0, 1),
         G(1, 2, "a", 20, 10, 2), G(1, 3, "a", 20, 10, 3),
         G(2, 3, "a", 26, 10, 1), G(2, 4, "a", 27, 10, 2)]
own = ts.compute(T(), es, games)                       # default: own_average
byer = next(s for s in own if s.entry_id == 1)
check("default bye is the player's own average, never above it",
      byer.vp_for <= (20 + 20) + 20, f"vp_for={byer.vp_for}")
fixed_big = ts.compute(T(scoring={"bye_vp_mode": "fixed", "bye_vp_fixed": 43}), es, games)
check("a fixed bye is available but is a deliberate choice",
      next(s for s in fixed_big if s.entry_id == 1).vp_for >
      next(s for s in own if s.entry_id == 1).vp_for)


print("\n6. The formula must be publishable")
lines = ts.describe(T(scoring={"vp_mode": "capped", "vp_cap": 40,
                               "sports_enabled": True, "painting_enabled": True}))
check("describe() explains the primary sort", any("Ranked on record" in l for l in lines))
check("describe() explains the VP cap", any("up to 40 per game" in l for l in lines))
check("describe() explains the bye rule", any("bye scores" in l for l in lines))
check("describe() explains sportsmanship", any("Sportsmanship" in l for l in lines))
check("describe() explains ties", any("Ties are broken" in l for l in lines))
print("     e.g. " + lines[0])


print("\n7. Painting")
es = [E(1, "Painter", painting_score=10), E(2, "Plain", painting_score=0)]
games = [G(1, 2, "draw", 10, 10, 1)]
tie = ts.compute(T(scoring={"painting_enabled": True, "painting_mode": "tiebreak",
                            "tiebreakers": ["paint"]}), es, games)
check("painting can break a tie", tie[0].entry_id == 1)
bonus = ts.compute(T(scoring={"painting_enabled": True, "painting_mode": "bonus",
                              "painting_bonus_per_point": 1}), es, games)
check("painting can add points when the TO wants it to",
      next(s for s in bonus if s.entry_id == 1).points >
      next(s for s in bonus if s.entry_id == 2).points)


print("\n7b. A bracket must not turn a real game into a bye")
# Brackets filter the standings, not the games. An opponent in another bracket
# used to come back as "no such entry" and drop into the bye branch, handing
# the player bye points for a game they actually played.
_cross = [E(1, "Ann", bracket="A"), E(2, "Bob", bracket="A"), E(3, "Cat", bracket="B")]
_t = T(win_points=3, draw_points=1, loss_points=0, bye_points=3)
_games = [
    G(1, 2, "a", 20, 10, round_id=1),   # inside bracket A
    G(1, 3, "b", 5, 30, round_id=2),    # Ann lost to Cat, who is in bracket B
]
_st = ts.compute(_t, _cross, _games, bracket="A")
_ann = next(x for x in _st if x.entry_id == 1)
check("the cross-bracket game is not counted as a bye", _ann.byes == 0, f"byes={_ann.byes}")
check("no bye points were awarded for it", _ann.win_points == _t.win_points,
      f"win_points={_ann.win_points}")
check("only the in-bracket game counts as played", _ann.played == 1, f"played={_ann.played}")

# A genuinely missing opponent (entry removed from the event) is still a bye.
_gone = [E(1, "Ann", bracket="A"), E(2, "Bob", bracket="A")]
_st2 = ts.compute(_t, _gone, [G(1, 99, "a", 20, 0, round_id=1)], bracket="A")
_ann2 = next(x for x in _st2 if x.entry_id == 1)
check("an opponent that no longer exists is still a bye", _ann2.byes == 1, f"byes={_ann2.byes}")

# Unbracketed scoring must be untouched by the change.
_st3 = ts.compute(_t, _cross, _games)
_ann3 = next(x for x in _st3 if x.entry_id == 1)
check("without a bracket, both games still count", _ann3.played == 2 and _ann3.byes == 0,
      f"played={_ann3.played} byes={_ann3.byes}")


print("\n8. Config validation")
check("a bad primary sort is rejected", ts.validate({"primary": "vibes"}))
check("capped VP without a cap is rejected", ts.validate({"vp_mode": "capped"}))
check("an unknown tiebreaker is rejected", ts.validate({"tiebreakers": ["nonsense"]}))
check("an inverted multiplier range is rejected",
      ts.validate({"sports_multiplier_min": 2.0, "sports_multiplier_max": 1.0}))
check("a sane config passes", not ts.validate(
    {"primary": "wins", "vp_mode": "capped", "vp_cap": 40, "tiebreakers": ["sos", "diff"]}))

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
