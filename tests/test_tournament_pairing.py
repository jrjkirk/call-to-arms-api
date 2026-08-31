"""Swiss pairing and standings, with the fixtures that break naive implementations.

Run: PYTHONPATH=. python tests/test_tournament_pairing.py
Deliberately dependency-free (no pytest in requirements) and DB-free — the
pairer takes plain objects so it can be exercised without a database.
"""
import random
import sys
from dataclasses import dataclass, field
from typing import Optional

import tournament_pairing as tp
import tournament_scoring as ts


@dataclass
class E:      # stands in for TournamentEntry
    id: int
    display_name: str
    status: str = "checked_in"
    seed: Optional[int] = None


@dataclass
class G:      # stands in for TournamentGame
    a_entry_id: int
    b_entry_id: Optional[int]
    result: Optional[str] = None
    a_score: int = 0
    b_score: int = 0


@dataclass
class T:      # stands in for Tournament
    win_points: int = 3
    draw_points: int = 1
    loss_points: int = 0
    bye_points: int = 3
    tiebreakers: Optional[list] = None


FAILURES = []

def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


def players(n):
    return [E(i, f"P{i}") for i in range(1, n + 1)]


def play(pairs, winner_of=lambda a, b: "a"):
    """Turn pairs into completed games; default: the first-named player wins."""
    out = []
    for p in pairs:
        if p.b_entry_id is None:
            out.append(G(p.a_entry_id, None, "bye"))
        else:
            out.append(G(p.a_entry_id, p.b_entry_id,
                         winner_of(p.a_entry_id, p.b_entry_id), 20, 10))
    return out


# ---------------------------------------------------------------------------
print("\nRound one")
# ---------------------------------------------------------------------------
t, es = T(), players(8)
pairs = tp.pair_round(t, es, [], 1, rng=random.Random(1))
check("8 players make 4 games", len(pairs) == 4, f"got {len(pairs)}")
seen = [i for p in pairs for i in (p.a_entry_id, p.b_entry_id) if i]
check("everyone plays exactly once", sorted(seen) == list(range(1, 9)), str(sorted(seen)))

es9 = players(9)
pairs = tp.pair_round(t, es9, [], 1, rng=random.Random(1))
byes = [p for p in pairs if p.b_entry_id is None]
check("odd field produces exactly one bye", len(byes) == 1, f"got {len(byes)}")
check("odd field still seats everyone", len(pairs) == 5, f"got {len(pairs)}")

# ---------------------------------------------------------------------------
print("\nNo rematches")
# ---------------------------------------------------------------------------
t, es = T(), players(8)
games = []
for rnd in range(1, 4):
    pairs = tp.pair_round(t, es, games, rnd, rng=random.Random(7))
    met = [frozenset((p.a_entry_id, p.b_entry_id)) for p in pairs if p.b_entry_id]
    already = tp._played_pairs(games)
    repeats = [m for m in met if m in already]
    check(f"round {rnd} has no rematch", not repeats, str(repeats))
    games += play(pairs)

# ---------------------------------------------------------------------------
print("\nByes are shared out")
# ---------------------------------------------------------------------------
t, es = T(), players(7)
games, bye_counts = [], {}
for rnd in range(1, 5):
    pairs = tp.pair_round(t, es, games, rnd, rng=random.Random(3))
    for p in pairs:
        if p.b_entry_id is None:
            bye_counts[p.a_entry_id] = bye_counts.get(p.a_entry_id, 0) + 1
    games += play(pairs)
check("nobody gets two byes while others have none",
      max(bye_counts.values()) <= 1 or len(bye_counts) == 7, str(bye_counts))

# ---------------------------------------------------------------------------
print("\nDrops")
# ---------------------------------------------------------------------------
t, es = T(), players(8)
games = play(tp.pair_round(t, es, [], 1, rng=random.Random(5)))
es[2].status = "dropped"          # P3 leaves after round one
pairs = tp.pair_round(t, es, games, 2, rng=random.Random(5))
ids = [i for p in pairs for i in (p.a_entry_id, p.b_entry_id) if i]
check("a dropped player is not paired again", 3 not in ids, str(ids))
check("the rest still all play", sorted(ids) == [1, 2, 4, 5, 6, 7, 8], str(sorted(ids)))
st = ts.compute(t, es, games)
p3 = next(s for s in st if s.entry_id == 3)
check("a dropped player keeps the games they played", p3.played == 1, f"played={p3.played}")

# ---------------------------------------------------------------------------
print("\nDeadlock: everyone has played everyone")
# ---------------------------------------------------------------------------
t, es = T(), players(4)
games = [G(1, 2, "a"), G(3, 4, "a"), G(1, 3, "a"), G(2, 4, "a"), G(1, 4, "a"), G(2, 3, "a")]
pairs = tp.pair_round(t, es, games, 4, rng=random.Random(2))
ids = [i for p in pairs for i in (p.a_entry_id, p.b_entry_id) if i]
check("still returns a full round rather than failing",
      len(pairs) == 2 and sorted(ids) == [1, 2, 3, 4], f"{pairs}")

# ---------------------------------------------------------------------------
print("\nStandings")
# ---------------------------------------------------------------------------
t, es = T(), players(4)
games = [G(1, 2, "a", 20, 5), G(3, 4, "draw", 10, 10)]
st = ts.compute(t, es, games)
check("winner tops the table", st[0].entry_id == 1, str([s.entry_id for s in st]))
check("win scores 3", st[0].points == 3, str(st[0].points))
check("draws score 1 each", all(s.points == 1 for s in st if s.entry_id in (3, 4)))
check("loser scores 0", next(s for s in st if s.entry_id == 2).points == 0)
check("differential is tracked", st[0].diff == 15, str(st[0].diff))

# a bye is worth its configured points, not a win over nobody
st = ts.compute(T(bye_points=2), es, [G(1, None, "bye")])
check("bye uses bye_points", next(s for s in st if s.entry_id == 1).points == 2)

# strength of schedule separates equal records
t = T()
es5 = players(5)
games = [G(1, 2, "a"), G(3, 4, "a"),      # 1 beat 2, 3 beat 4
         G(2, 5, "a"), G(4, 5, "b")]      # 2 beat 5; 5 beat 4
st = ts.compute(t, es5, games)
one = next(s for s in st if s.entry_id == 1)
three = next(s for s in st if s.entry_id == 3)
check("equal records are split by strength of schedule",
      one.points == three.points and one.sos != three.sos,
      f"1: {one.points}/{one.sos}  3: {three.points}/{three.sos}")

# unplayed games contribute nothing, so standings are safe mid-round
st = ts.compute(t, players(2), [G(1, 2, None)])
check("an unplayed game scores nothing", all(s.points == 0 for s in st))

# ---------------------------------------------------------------------------
print("\nA whole 5-round, 17-player event")
# ---------------------------------------------------------------------------
t, es = T(), players(17)
games = []
rng = random.Random(11)
for rnd in range(1, 6):
    pairs = tp.pair_round(t, es, games, rnd, rng=rng)
    ids = [i for p in pairs for i in (p.a_entry_id, p.b_entry_id) if i]
    if len(ids) != len(set(ids)):
        check(f"round {rnd}: nobody is double-booked", False, str(sorted(ids)))
        break
    if len(ids) != 17:
        check(f"round {rnd}: everyone is accounted for", False, f"{len(ids)} of 17")
        break
    games += play(pairs, winner_of=lambda a, b: rng.choice(["a", "b", "draw"]))
else:
    check("5 rounds x 17 players stays consistent", True)
    counts = {}
    for g in games:
        if g.b_entry_id is None:
            counts[g.a_entry_id] = counts.get(g.a_entry_id, 0) + 1
    check("no player gets more than one bye across the event",
          not counts or max(counts.values()) == 1, str(counts))
    st = ts.compute(t, es, games)
    check("standings cover every player", len(st) == 17, str(len(st)))
    check("standings are ordered by points",
          all(st[i].points >= st[i+1].points for i in range(len(st)-1)))

# ---------------------------------------------------------------------------
print("\nFuzz: 500 random events")
# ---------------------------------------------------------------------------
# The unit cases above all pass ALL entries to compute(); the pairer used to
# pass only the active ones, so a game played by someone who later dropped
# crashed the standings. Nothing above caught it and this did — random events
# with mid-event drops are worth more here than any number of hand-picked ones.
fuzz_bad = []
for trial in range(500):
    rng = random.Random(trial)
    n, rounds = rng.randint(2, 40), rng.randint(1, 6)
    t = T(bye_points=rng.choice([0, 1, 3]))
    es = [E(i, f"P{i}") for i in range(1, n + 1)]
    games = []
    for rnd in range(1, rounds + 1):
        if rnd > 1:
            for e in es:
                if e.status == "checked_in" and rng.random() < 0.05:
                    e.status = "dropped"
        active = {e.id for e in es if e.status == "checked_in"}
        pairs = tp.pair_round(t, es, games, rnd, rng=rng)
        ids = [i for p in pairs for i in (p.a_entry_id, p.b_entry_id) if i]
        if len(ids) != len(set(ids)):
            fuzz_bad.append((trial, rnd, "double-booked")); break
        if len(active) >= 2 and set(ids) != active:
            fuzz_bad.append((trial, rnd, "active player missing")); break
        if len([p for p in pairs if p.b_entry_id is None]) > 1:
            fuzz_bad.append((trial, rnd, "more than one bye")); break
        games += play(pairs, winner_of=lambda a, b: rng.choice(["a", "b", "draw"]))
    else:
        st = ts.compute(t, es, games)
        if len(st) != n:
            fuzz_bad.append((trial, "-", "standings lost a player"))
        elif any(st[i].points < st[i + 1].points for i in range(len(st) - 1)):
            fuzz_bad.append((trial, "-", "standings out of order"))

check("500 random events hold every invariant", not fuzz_bad, str(fuzz_bad[:5]))

print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
sys.exit(1 if FAILURES else 0)
