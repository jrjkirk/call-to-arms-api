"""What a vibe is, and how it behaves when the matcher looks at it.

A vibe used to be a name and nothing else, from a fixed list of five, and every
one of them meant the same thing to the matcher: a soft preference costing
`weight_vibe` when two players disagreed. Two were quietly special-cased in the
engine anyway ("Open" matched anything at zero cost, "Intro" had its own rule
and a whole pre-pass), so the idea that vibes can behave differently was already
there. It just wasn't something a club could reach.

Clubs asked for it. The Old World wants a "Battle March" option: a smaller
points game, so those players must only meet each other. That is not a
preference, it is a partition, and no amount of turning the vibe weight up
expresses it.

So a vibe now carries a behaviour:

  soft       Casual, Competitive. Costs the vibe weight when mismatched.
             What every vibe did before, and still the default.
  wildcard   Open. Matches anything at zero cost.
  exclusive  Battle March. Only pairs with the same vibe.

**Exclusive beats wildcard, deliberately.** Someone who picked "Open" is saying
they will play anyone, not that they brought a second army at a different points
level. Letting a wildcard satisfy an exclusive vibe would put them in a game they
cannot play.

Storage stays backward compatible: `ClubSystem.vibe_options` and
`SystemConfig.vibe_options` are JSON lists that may hold plain strings (the old
shape, meaning soft) or objects `{"name": ..., "behaviour": ...}`. Nothing has to
migrate, and a club that never opens the new editor is unaffected.
"""
from typing import Iterable, NamedTuple, Optional

SOFT = "soft"
WILDCARD = "wildcard"
EXCLUSIVE = "exclusive"
BEHAVIOURS = (SOFT, WILDCARD, EXCLUSIVE)

# The two the engine special-cased by name long before this existed. Keeping
# them here means a club that lists plain strings still gets the behaviour it
# always had, without anyone having to re-declare it.
#
# "Either" is "Open" under its pre-2026-08-28 name; historical signups still
# carry it, and a flexible player must stay flexible in old weeks too.
_IMPLIED_BY_NAME = {
    "open": WILDCARD,
    "either": WILDCARD,
}


class VibeSpec(NamedTuple):
    name: str
    behaviour: str

    @property
    def key(self) -> str:
        return self.name.strip().lower()


def parse(raw: Optional[Iterable]) -> list[VibeSpec]:
    """Read a stored vibe list, in either shape.

    Unknown behaviours fall back to soft rather than raising: this reads config
    that may have been written by an older or newer version of the app, and a
    vibe that behaves conservatively is better than a pairing run that dies.
    """
    out: list[VibeSpec] = []
    for item in raw or []:
        if isinstance(item, str):
            name = item.strip()
            behaviour = _IMPLIED_BY_NAME.get(name.lower(), SOFT)
        elif isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            behaviour = str(item.get("behaviour", "") or "").strip().lower()
            if behaviour not in BEHAVIOURS:
                behaviour = _IMPLIED_BY_NAME.get(name.lower(), SOFT)
        else:
            continue
        if name and not any(v.key == name.lower() for v in out):
            out.append(VibeSpec(name, behaviour))
    return out


def names(specs: Iterable[VibeSpec]) -> list[str]:
    return [v.name for v in specs]


def to_storage(specs: Iterable[VibeSpec]) -> list:
    """What goes in the JSON column.

    Plain strings for anything a name already implies, objects only where the
    behaviour has to be stated. Keeps existing rows byte-identical when a club
    edits something unrelated, and keeps the column readable.
    """
    out = []
    for v in specs:
        if v.behaviour == _IMPLIED_BY_NAME.get(v.key, SOFT):
            out.append(v.name)
        else:
            out.append({"name": v.name, "behaviour": v.behaviour})
    return out


def behaviour_of(vibe: Optional[str], specs: Iterable[VibeSpec]) -> str:
    """How a signup's stored vibe string behaves.

    An unrecognised vibe is soft. Signups keep whatever string was current when
    they were made, so a club renaming or dropping a vibe must not change how
    last month's pairings would be scored.
    """
    key = (vibe or "").strip().lower()
    if not key:
        return SOFT
    for v in specs:
        if v.key == key:
            return v.behaviour
    return _IMPLIED_BY_NAME.get(key, SOFT)


def incompatible(a_vibe: Optional[str], b_vibe: Optional[str], specs: Iterable[VibeSpec]) -> bool:
    """True when these two must not be paired because of an exclusive vibe.

    Exclusivity is mutual and one-sided at once: if EITHER player has an
    exclusive vibe, the other must have the same one. A Battle March player
    needs a Battle March opponent, and a standard player must not be pulled
    into a Battle March game they have no army for.
    """
    a_key = (a_vibe or "").strip().lower()
    b_key = (b_vibe or "").strip().lower()
    a_ex = behaviour_of(a_vibe, specs) == EXCLUSIVE
    b_ex = behaviour_of(b_vibe, specs) == EXCLUSIVE
    if not a_ex and not b_ex:
        return False
    return a_key != b_key
