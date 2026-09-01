"""Standings for a tournament: configurable, and transparent about it.

Derived, never stored. Recomputing from games is cheap at tournament scale and
removes the class of bug where a stored standings table disagrees with the
games it came from.

The defaults here are opinionated, and the opinion comes from a written
post-event review of an event scored the other way. The failure modes it
documented, and what this does about each:

  A 2-2 player finished above a 4-0 player, because total VP was the primary
  sort. Here `primary` defaults to "wins" — the one part of a result every
  player and spectator can read the same way without knowing the formula.

  One blowout round outweighed a whole event of consistent scoring, because VP
  was uncapped. `vp_mode` offers "capped" and "normalised" (each round scored
  against that round's field average) so no single round can dominate.

  A 0.2 difference in a one-click sportsmanship rating moved a player six
  places, because it multiplied the whole total. Sportsmanship defaults to off,
  and when on defaults to a tiebreaker rather than a multiplier; the multiplier
  mode exists but its range is bounded.

  A single sore-loser rating swung a player's whole multiplier.
  `sports_drop_lowest` drops each player's worst rating before averaging.

  A bye was worth more VP than the round's field average, with no published
  formula. `bye_vp_mode` defaults to that player's own average across the
  rounds they actually played, and describe() states which rule is in force so
  it can be printed next to the standings.
"""
from dataclasses import dataclass, field
from typing import Optional

# Tiebreakers, applied in the order the TO chooses, after the primary sort.
#   wins    games won (useful as a tiebreak when primary is VP)
#   vp      total victory points, after vp_mode is applied
#   sos     strength of schedule — the average score of everyone you played
#   diff    victory point differential
#   sports  average sportsmanship received
#   paint   painting score
#   h2h     head to head, only between two tied players who met
TIEBREAKERS = ("wins", "vp", "sos", "diff", "sports", "paint", "h2h")

# Primary sort keys.
#   wins       tournament points from win/draw/loss. The default, and the one
#              players can read without the formula in front of them.
#   vp         total victory points
#   composite  win points plus VP, for TOs who genuinely want both to count
PRIMARY = ("wins", "vp", "composite")

DEFAULT_SCORING = {
    "primary": "wins",
    "tiebreakers": ["sos", "diff", "vp"],

    # raw | capped | normalised
    #   capped     — each game's VP counted only up to vp_cap
    #   normalised — each game scored against that round's field average, so a
    #                blowout in a low-scoring round can't outweigh a full event
    "vp_mode": "raw",
    "vp_cap": None,

    # fixed | field_average | own_average
    "bye_vp_mode": "own_average",
    "bye_vp_fixed": 0,

    "sports_enabled": False,
    "sports_scale_max": 5,
    "sports_mode": "tiebreak",        # tiebreak | multiplier | bonus
    "sports_multiplier_min": 0.9,     # bounded on purpose — see the docstring
    "sports_multiplier_max": 1.1,
    "sports_bonus_per_point": 1,
    "sports_drop_lowest": True,

    "painting_enabled": False,
    "painting_max": 10,
    "painting_mode": "tiebreak",      # tiebreak | bonus
    "painting_bonus_per_point": 1,
}


def config(tournament) -> dict:
    """The event's scoring policy, with any missing key filled from defaults —
    so an event created before a knob existed keeps working."""
    cfg = dict(DEFAULT_SCORING)
    cfg.update(tournament.scoring or {})
    if tournament.tiebreakers:
        cfg["tiebreakers"] = tournament.tiebreakers
    return cfg


def validate(patch: dict) -> list[str]:
    """Problems with a proposed scoring config, as readable sentences."""
    errs = []
    if "primary" in patch and patch["primary"] not in PRIMARY:
        errs.append(f"Primary sort must be one of: {', '.join(PRIMARY)}.")
    if "vp_mode" in patch and patch["vp_mode"] not in ("raw", "capped", "normalised"):
        errs.append("VP mode must be raw, capped or normalised.")
    if patch.get("vp_mode") == "capped" and not patch.get("vp_cap"):
        errs.append("Capped VP needs a per-game cap.")
    if "bye_vp_mode" in patch and patch["bye_vp_mode"] not in ("fixed", "field_average", "own_average"):
        errs.append("Bye VP must be fixed, field_average or own_average.")
    for k in ("sports_mode",):
        if k in patch and patch[k] not in ("tiebreak", "multiplier", "bonus"):
            errs.append("Sportsmanship mode must be tiebreak, multiplier or bonus.")
    if "painting_mode" in patch and patch["painting_mode"] not in ("tiebreak", "bonus"):
        errs.append("Painting mode must be tiebreak or bonus.")
    for tb in patch.get("tiebreakers", []) or []:
        if tb not in TIEBREAKERS:
            errs.append(f"Unknown tiebreaker '{tb}'. Pick from: {', '.join(TIEBREAKERS)}.")
    lo = patch.get("sports_multiplier_min")
    hi = patch.get("sports_multiplier_max")
    if lo is not None and hi is not None and lo > hi:
        errs.append("Sportsmanship multiplier minimum cannot exceed its maximum.")
    return errs


def describe(tournament) -> list[str]:
    """How this event is scored, in plain sentences, for printing next to the
    standings. The review that shaped this module was explicit that an
    unpublished formula is itself a problem — players could not check the
    result against a rule they were never shown."""
    c = config(tournament)
    out = []
    if c["primary"] == "wins":
        out.append(f"Ranked on record first: {tournament.win_points} for a win, "
                   f"{tournament.draw_points} for a draw, {tournament.loss_points} for a loss.")
    elif c["primary"] == "vp":
        out.append("Ranked on total victory points.")
    else:
        out.append("Ranked on record and victory points combined.")

    if c["vp_mode"] == "capped":
        out.append(f"Victory points count up to {c['vp_cap']} per game, so one "
                   f"blowout can't outweigh a consistent event.")
    elif c["vp_mode"] == "normalised":
        out.append("Each game is scored on where it placed within that round's "
                   "field, out of 100, so every round is worth the same and one "
                   "big score can't outweigh a consistent event.")

    bye = {"own_average": "a player's own average across the rounds they played",
           "field_average": "that round's field average",
           "fixed": f"{c['bye_vp_fixed']} VP"}[c["bye_vp_mode"]]
    out.append(f"A bye scores {bye}.")

    if c["sports_enabled"]:
        how = {"tiebreak": "used only to separate players who are otherwise level",
               "multiplier": f"applied as a multiplier between "
                             f"{c['sports_multiplier_min']}x and {c['sports_multiplier_max']}x",
               "bonus": f"worth {c['sports_bonus_per_point']} point(s) per point"}[c["sports_mode"]]
        line = f"Sportsmanship (out of {c['sports_scale_max']}) is {how}."
        if c["sports_drop_lowest"]:
            line += " Each player's lowest rating is dropped before averaging."
        out.append(line)

    if c["painting_enabled"]:
        how = ("used only as a tiebreaker" if c["painting_mode"] == "tiebreak"
               else f"worth {c['painting_bonus_per_point']} point(s) per point")
        out.append(f"Painting (out of {c['painting_max']}) is {how}.")

    if c["tiebreakers"]:
        names = {"wins": "games won", "vp": "victory points",
                 "sos": "strength of schedule", "diff": "points differential",
                 "sports": "sportsmanship", "paint": "painting", "h2h": "head to head"}
        out.append("Ties are broken by " +
                   ", then ".join(names.get(t, t) for t in c["tiebreakers"]) + ".")
    return out


@dataclass
class Standing:
    entry_id: int
    name: str
    bracket: Optional[str] = None
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    byes: int = 0
    win_points: int = 0
    vp_for: float = 0.0
    vp_against: float = 0.0
    raw_vp: int = 0
    sports_scores: list = field(default_factory=list)
    painting: Optional[int] = None
    opponents: list = field(default_factory=list)
    dropped: bool = False
    points: float = 0.0          # the number the table is sorted on
    sos: float = 0.0
    sports_avg: Optional[float] = None

    @property
    def diff(self) -> float:
        return self.vp_for - self.vp_against


def _round_scores(games) -> dict:
    """Every VP scored in each round, so a score can be judged against the field
    that actually played that round."""
    out = {}
    for g in games:
        if not g.result or g.result == "bye" or not g.b_entry_id:
            continue
        out.setdefault(g.round_id, []).extend([g.a_score or 0, g.b_score or 0])
    return {r: sorted(v) for r, v in out.items()}


def _round_averages(scores: dict) -> dict:
    return {r: (sum(v) / len(v)) if v else 0.0 for r, v in scores.items()}


def _percentile(score: int, field: list) -> float:
    """Where this score sits in its own round, 0-100.

    This is what "normalised" means here, and the choice matters. Subtracting
    the round average — the obvious reading — re-centres scores but does not
    bound them: a 90 in a round averaging 34 is still worth +56, so one blowout
    still outweighs a whole event of consistency, which is the exact complaint
    normalisation was meant to answer. A percentile caps every round at 100, so
    each round is worth the same and being the best in a round is the most it
    can ever be worth.
    """
    if not field:
        return 0.0
    at_or_below = sum(1 for x in field if x <= score)
    return 100.0 * at_or_below / len(field)


def compute(tournament, entries, games, bracket: Optional[str] = None) -> list[Standing]:
    """Standings, best first. Unplayed games contribute nothing, so this is safe
    to call mid-round while half the tables are still going."""
    c = config(tournament)
    # Captured BEFORE the bracket filter, so the loop below can tell "this
    # opponent is in another bracket" apart from "this opponent isn't in the
    # event at all". Those need opposite handling and used to get the same one.
    all_entry_ids = {e.id for e in entries}
    if bracket is not None:
        entries = [e for e in entries if (e.bracket or None) == bracket]

    table = {
        e.id: Standing(entry_id=e.id, name=e.display_name, bracket=e.bracket,
                       dropped=e.status == "dropped", painting=e.painting_score)
        for e in entries
    }
    round_scores = _round_scores(games)
    averages = _round_averages(round_scores)

    def vp(raw: int, round_id) -> float:
        if c["vp_mode"] == "capped" and c["vp_cap"]:
            return min(raw, c["vp_cap"])
        if c["vp_mode"] == "normalised":
            return _percentile(raw, round_scores.get(round_id, []))
        return raw

    byes = []
    for g in games:
        if not g.result:
            continue
        a = table.get(g.a_entry_id)
        b = table.get(g.b_entry_id) if g.b_entry_id else None
        if a is None:
            continue

        # An opponent who exists but was filtered out by `bracket` played a
        # real game — it just isn't part of THIS bracket's standings. Skipping
        # it is the only honest option: counting it would score an opponent
        # who isn't ranked here, and the previous behaviour (falling through to
        # the bye branch) handed the player bye points for a game they played.
        if b is None and g.b_entry_id in all_entry_ids:
            continue

        # A genuine bye: recorded as one, no opponent at all, or an opponent
        # whose entry has since been removed from the event.
        if g.result == "bye" or b is None:
            a.byes += 1
            a.win_points += tournament.bye_points
            byes.append((a, g.round_id))
            continue

        a.played += 1; b.played += 1
        a.opponents.append(b.entry_id); b.opponents.append(a.entry_id)
        a.raw_vp += g.a_score or 0
        b.raw_vp += g.b_score or 0
        a.vp_for += vp(g.a_score or 0, g.round_id)
        a.vp_against += vp(g.b_score or 0, g.round_id)
        b.vp_for += vp(g.b_score or 0, g.round_id)
        b.vp_against += vp(g.a_score or 0, g.round_id)
        if g.a_sports is not None:
            a.sports_scores.append(g.a_sports)
        if g.b_sports is not None:
            b.sports_scores.append(g.b_sports)

        if g.result == "a":
            a.wins += 1; b.losses += 1
            a.win_points += tournament.win_points; b.win_points += tournament.loss_points
        elif g.result == "b":
            b.wins += 1; a.losses += 1
            b.win_points += tournament.win_points; a.win_points += tournament.loss_points
        elif g.result == "draw":
            a.draws += 1; b.draws += 1
            a.win_points += tournament.draw_points; b.win_points += tournament.draw_points

    # A bye's VP can only be worked out once real games are counted, because
    # two of the three rules refer to scores earned elsewhere.
    for s, round_id in byes:
        if c["bye_vp_mode"] == "fixed":
            s.vp_for += c["bye_vp_fixed"]
        elif c["bye_vp_mode"] == "field_average":
            s.vp_for += averages.get(round_id, 0.0)
        else:  # own_average — never more than the player earned themselves
            s.vp_for += (s.vp_for / s.played) if s.played else 0.0

    # Sportsmanship, with the lowest rating dropped so one sore-loser click
    # can't swing a whole event.
    for s in table.values():
        scores = sorted(s.sports_scores)
        if c["sports_drop_lowest"] and len(scores) > 1:
            scores = scores[1:]
        s.sports_avg = round(sum(scores) / len(scores), 2) if scores else None

    sos = {
        s.entry_id: (sum(table[o].win_points for o in s.opponents if o in table) / len(s.opponents))
        if s.opponents else 0.0
        for s in table.values()
    }
    for s in table.values():
        s.sos = round(sos[s.entry_id], 2)

    # The primary number the table is sorted on.
    for s in table.values():
        if c["primary"] == "wins":
            s.points = s.win_points
        elif c["primary"] == "vp":
            s.points = s.vp_for
        else:
            s.points = s.win_points + s.vp_for

        if c["sports_enabled"] and s.sports_avg is not None:
            if c["sports_mode"] == "multiplier":
                span = c["sports_multiplier_max"] - c["sports_multiplier_min"]
                frac = s.sports_avg / max(c["sports_scale_max"], 1)
                s.points *= c["sports_multiplier_min"] + span * frac
            elif c["sports_mode"] == "bonus":
                s.points += s.sports_avg * c["sports_bonus_per_point"]
        if c["painting_enabled"] and c["painting_mode"] == "bonus" and s.painting:
            s.points += s.painting * c["painting_bonus_per_point"]
        s.points = round(s.points, 3)

    def key(s: Standing):
        parts = [s.points]
        for tb in c["tiebreakers"]:
            if tb == "wins": parts.append(s.wins)
            elif tb == "vp": parts.append(s.vp_for)
            elif tb == "sos": parts.append(sos[s.entry_id])
            elif tb == "diff": parts.append(s.diff)
            elif tb == "sports": parts.append(s.sports_avg or 0)
            elif tb == "paint": parts.append(s.painting or 0)
        return tuple(parts)

    ranked = sorted(table.values(), key=key, reverse=True)
    for s in ranked:
        s._rank_key = key(s)
    if "h2h" in c["tiebreakers"]:
        ranked = _apply_head_to_head(ranked, games)
    return ranked


def _apply_head_to_head(ranked: list[Standing], games) -> list[Standing]:
    """Swap two ADJACENT players level on everything else who played each other.

    Only adjacent pairs, because head to head is incoherent for three or more
    (A beat B beat C beat A) and applying it more widely gives an order that
    depends on which pair you compared first.
    """
    beat = {}
    for g in games:
        if g.result in ("a", "b") and g.b_entry_id:
            w = g.a_entry_id if g.result == "a" else g.b_entry_id
            l = g.b_entry_id if g.result == "a" else g.a_entry_id
            beat.setdefault(w, set()).add(l)

    out = list(ranked)
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if getattr(a, "_rank_key", None) != getattr(b, "_rank_key", None):
            continue
        if b.entry_id in beat.get(a.entry_id, ()):
            continue
        if a.entry_id in beat.get(b.entry_id, ()):
            out[i], out[i + 1] = b, a
    return out
