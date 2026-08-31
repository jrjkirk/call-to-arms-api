"""Swiss pairing for tournaments.

Deliberately NOT pairings_engine.py, and deliberately not a mode of it.

That engine exists to make a club night pleasant: it weighs mirror factions,
rematch history, vibe, experience and points, and treats "don't replay last
week's opponent" as a hard rule. It is a social matcher, CLAUDE.md ring-fences
it, and it should stay ring-fenced.

Swiss inverts those priorities. Players are paired BECAUSE their records match,
inside score brackets, with rematches avoided across the whole event rather
than one week, floating down when a bracket is odd. That is a different
function with different inputs, not a different set of weights.

Where this kind of code usually goes wrong, and what is done about it here:

  byes      go to the lowest-ranked active player who has not had one, so the
            same person never gets two while someone else gets none.
  drops     stop a player being paired from the next round on. Their played
            games stand, which is what every TO expects.
  deadlock  a bracket where everyone has already played everyone. Rather than
            failing, the rematch rule is relaxed as a last resort — a repeat
            pairing is bad, and no round at all is worse.
"""
import random
from dataclasses import dataclass
from typing import Optional

import tournament_scoring as scoring


@dataclass
class Pair:
    a_entry_id: int
    b_entry_id: Optional[int]      # None = bye
    bracket: Optional[int] = None  # tournament points the pair came from


def _played_pairs(games) -> set:
    """Every unordered pair that has already met, so a rematch is one lookup."""
    seen = set()
    for g in games:
        if g.b_entry_id:
            seen.add(frozenset((g.a_entry_id, g.b_entry_id)))
    return seen


def _had_bye(games) -> set:
    return {g.a_entry_id for g in games if g.b_entry_id is None or g.result == "bye"}


def pair_round(
    tournament,
    entries,
    games,
    round_no: int,
    *,
    rng: Optional[random.Random] = None,
) -> list[Pair]:
    """Pair one round. Returns the pairs; persisting them is the caller's job.

    Round one has no records to sort on, so it uses the TO's seeds where set and
    random order otherwise. Later rounds sort by the same standings players see,
    which is what makes the top table mean what it looks like.
    """
    rng = rng or random.Random()

    # Only checked-in players are paired. Someone who registered and never
    # turned up must not be given an opponent who then sits alone for an hour.
    active = [e for e in entries if e.status == "checked_in"]
    if len(active) < 2:
        return []

    if round_no <= 1:
        seeded = [e for e in active if e.seed is not None]
        unseeded = [e for e in active if e.seed is None]
        seeded.sort(key=lambda e: e.seed)
        rng.shuffle(unseeded)
        ordered = [e.id for e in seeded + unseeded]
        brackets = {None: ordered}
    else:
        # ALL entries, not just active ones: the games include those played by
        # people who have since dropped, and their opponents' records depend on
        # them. Brackets are filtered to active players immediately below, so a
        # dropped player is scored but never paired.
        standings = scoring.compute(tournament, entries, games)
        active_ids = {e.id for e in active}
        brackets = {}
        for s in standings:
            if s.entry_id in active_ids:
                brackets.setdefault(s.points, []).append(s.entry_id)

    played = _played_pairs(games)
    had_bye = _had_bye(games)

    pairs: list[Pair] = []
    floater: Optional[int] = None          # odd one floated down from above

    for bracket_points in brackets:
        pool = list(brackets[bracket_points])
        if floater is not None:
            pool.insert(0, floater)
            floater = None

        matched, leftover = _pair_pool(pool, played, allow_rematch=False)
        if leftover:
            # Couldn't place everyone cleanly. Float the last one down to the
            # next bracket rather than forcing a rematch here — a slightly
            # mismatched pairing beats a repeat.
            floater = leftover[-1]
            for extra in leftover[:-1]:
                matched.append((extra, None))
        for a, b in matched:
            pairs.append(Pair(a, b, bracket_points))

    # Anything still unpaired at the bottom: retry allowing rematches, then bye.
    if floater is not None:
        pairs.append(Pair(floater, None, None))

    pairs = _resolve_byes(pairs, had_bye, tournament)
    return pairs


def _pair_pool(pool: list[int], played: set, *, allow_rematch: bool):
    """Greedily pair a score bracket in order, skipping rematches.

    Greedy rather than optimal on purpose. A perfect maximum matching would pair
    marginally better in rare cases and would be far harder for a TO to argue
    with; walking the list in standings order gives the "top of the bracket
    plays the next one down" behaviour people expect to see.
    """
    remaining = list(pool)
    matched = []
    while len(remaining) >= 2:
        a = remaining.pop(0)
        opponent_idx = None
        for i, b in enumerate(remaining):
            if allow_rematch or frozenset((a, b)) not in played:
                opponent_idx = i
                break
        if opponent_idx is None:
            # Everyone left has already played a. Try again allowing repeats
            # rather than leaving the bracket unpaired.
            if not allow_rematch:
                rest, left = _pair_pool([a] + remaining, played, allow_rematch=True)
                return matched + rest, left
            opponent_idx = 0
        matched.append((a, remaining.pop(opponent_idx)))
    return matched, remaining


def _resolve_byes(pairs: list[Pair], had_bye: set, tournament) -> list[Pair]:
    """At most one bye, and it goes to someone who has not had one.

    _pair_pool can leave more than one player unmatched across brackets. Those
    are collected and re-paired against each other, because two people sitting
    out is never right when they could play each other.
    """
    solo = [p for p in pairs if p.b_entry_id is None]
    if len(solo) <= 1:
        return _prefer_byeless(pairs, had_bye)

    rest = [p for p in pairs if p.b_entry_id is not None]
    ids = [p.a_entry_id for p in solo]
    while len(ids) >= 2:
        rest.append(Pair(ids.pop(0), ids.pop(0), None))
    if ids:
        rest.append(Pair(ids[0], None, None))
    return _prefer_byeless(rest, had_bye)


def _prefer_byeless(pairs: list[Pair], had_bye: set) -> list[Pair]:
    """If the bye landed on someone who already had one, swap it with the
    lowest-ranked player who hasn't. Pairs are in standings order, so walking
    backwards finds the lowest-ranked candidate first."""
    bye = next((p for p in pairs if p.b_entry_id is None), None)
    if bye is None or bye.a_entry_id not in had_bye:
        return pairs

    for p in reversed(pairs):
        if p.b_entry_id is None:
            continue
        for slot in ("a_entry_id", "b_entry_id"):
            candidate = getattr(p, slot)
            if candidate not in had_bye:
                setattr(p, slot, bye.a_entry_id)
                bye.a_entry_id = candidate
                return pairs
    # Everyone has had a bye. Nothing to fix.
    return pairs
