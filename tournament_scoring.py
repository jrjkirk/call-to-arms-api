"""Standings for a tournament: tournament points, then tiebreakers.

Derived, never stored. Recomputing from games is cheap at tournament scale (a
40-player five-round event is 100 games) and it removes the whole class of bug
where a stored standings table and the games it came from disagree.

Tiebreakers are ordered and configurable per event because systems and TOs
genuinely disagree about them, and it is exactly the sort of thing a TO wants
to set before the day rather than argue about after it.
"""
from dataclasses import dataclass, field
from typing import Optional

# Ordered tiebreakers a TO can pick from, applied after tournament points.
#   sos   strength of schedule — the average score of everyone you played.
#         The standard "did you earn it against real opposition" measure.
#   diff  total victory-point differential across your games.
#   vp    total victory points scored, ignoring what you conceded.
#   h2h   head to head, only meaningful between two tied players who met.
TIEBREAKERS = ("sos", "diff", "vp", "h2h")
DEFAULT_TIEBREAKERS = ("sos", "diff", "vp")


@dataclass
class Standing:
    entry_id: int
    name: str
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    byes: int = 0
    points: int = 0          # tournament points
    vp_for: int = 0
    vp_against: int = 0
    opponents: list = field(default_factory=list)   # entry_ids, byes excluded
    dropped: bool = False

    @property
    def diff(self) -> int:
        return self.vp_for - self.vp_against

    @property
    def rank_key(self):
        """Set by compute() once strength of schedule is known."""
        return getattr(self, "_rank_key", ())


def _blank(entry) -> Standing:
    return Standing(entry_id=entry.id, name=entry.display_name,
                    dropped=entry.status == "dropped")


def compute(tournament, entries, games, tiebreakers=None) -> list[Standing]:
    """Standings, best first.

    `games` may include unplayed ones — a game with no result contributes
    nothing, which is what makes this safe to call mid-round while half the
    tables are still going.
    """
    order = tuple(tiebreakers or tournament.tiebreakers or DEFAULT_TIEBREAKERS)
    table = {e.id: _blank(e) for e in entries}

    for g in games:
        if not g.result:
            continue
        a = table.get(g.a_entry_id)
        b = table.get(g.b_entry_id) if g.b_entry_id else None

        # A game can reference someone outside `entries` — a caller passing a
        # filtered list, or an entry deleted after its games were played. Skip
        # rather than crash: a missing player must not take the standings down
        # mid-event, which is the worst possible moment for it.
        if a is None:
            continue

        if g.result == "bye" or (g.b_entry_id and b is None) or b is None:
            a.byes += 1
            a.points += tournament.bye_points
            continue

        a.played += 1
        b.played += 1
        a.opponents.append(b.entry_id)
        b.opponents.append(a.entry_id)
        a.vp_for += g.a_score or 0
        a.vp_against += g.b_score or 0
        b.vp_for += g.b_score or 0
        b.vp_against += g.a_score or 0

        if g.result == "a":
            a.wins += 1; b.losses += 1
            a.points += tournament.win_points; b.points += tournament.loss_points
        elif g.result == "b":
            b.wins += 1; a.losses += 1
            b.points += tournament.win_points; a.points += tournament.loss_points
        elif g.result == "draw":
            a.draws += 1; b.draws += 1
            a.points += tournament.draw_points; b.points += tournament.draw_points

    # Strength of schedule needs every opponent's final points, so it can only
    # be worked out once the loop above has finished.
    sos = {
        s.entry_id: (sum(table[o].points for o in s.opponents if o in table) / len(s.opponents))
        if s.opponents else 0.0
        for s in table.values()
    }

    def key(s: Standing):
        parts = [s.points]
        for tb in order:
            if tb == "sos":
                parts.append(sos[s.entry_id])
            elif tb == "diff":
                parts.append(s.diff)
            elif tb == "vp":
                parts.append(s.vp_for)
            # h2h can't be expressed as a sort key — applied below.
        return tuple(parts)

    ranked = sorted(table.values(), key=key, reverse=True)
    for s in ranked:
        s._rank_key = key(s)
        s.sos = round(sos[s.entry_id], 2)

    if "h2h" in order:
        ranked = _apply_head_to_head(ranked, games)
    return ranked


def _apply_head_to_head(ranked: list[Standing], games) -> list[Standing]:
    """Swap two adjacent players who are level on every other measure and played
    each other. Deliberately only adjacent pairs: head to head is incoherent for
    groups of three or more (A beat B beat C beat A), so applying it more widely
    would produce an order that depends on which pair you looked at first.
    """
    won_against = {}
    for g in games:
        if g.result in ("a", "b") and g.b_entry_id:
            winner = g.a_entry_id if g.result == "a" else g.b_entry_id
            loser = g.b_entry_id if g.result == "a" else g.a_entry_id
            won_against.setdefault(winner, set()).add(loser)

    out = list(ranked)
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if a.rank_key != b.rank_key:
            continue
        if b.entry_id in won_against.get(a.entry_id, ()):
            continue
        if a.entry_id in won_against.get(b.entry_id, ()):
            out[i], out[i + 1] = b, a
    return out
