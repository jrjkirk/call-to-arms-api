"""What a club is allowed to change about a system it runs.

`SystemConfig` is the platform catalogue: one row per game system, shared by
every club. `ClubSystem` is one club's relationship with one of those systems.
Until now the only thing a club could say about how its own night ran was the
vibe list (and, through PairingConfig, the rematch windows) — everything else
about the signup form was decided once, centrally, for everybody.

That was wrong in a specific way. Two clubs running The Old World do not agree
on 2000 points, and a club that plays Kill Team to a scenario pack has no way
to say so. These are not platform decisions, they are the shape of a Thursday
evening, and the person who knows the answer is that system's own admin.

## The resolution rule, and why NULL is doing the work

Every overridable column on ClubSystem is nullable, and NULL means "no opinion,
use the catalogue". That is the same rule ClubSystem.vibe_options already
follows, and it matters for one reason: it keeps the platform default LIVE. A
club that has never touched these follows the catalogue, so a platform admin
correcting a system's max points still reaches them. Copying the catalogue
values into every ClubSystem row at migration time would have looked equivalent
and quietly frozen every club at the values of the day they were created.

## Read this through EffectiveSystem or not at all

The overrides have to reach the *matcher*, not just the form. A club that turns
scenarios off but leaves `pairings_engine` reading the catalogue gets a form
with no scenario field and a matcher still scoring scenario agreement between
two blank values. So `generate()` resolves the system through here too, and the
whole point of EffectiveSystem proxying every other attribute is that the
engine could keep saying `config.uses_scenarios` unchanged.

Anything that reads one of OVERRIDABLE_FIELDS off a raw SystemConfig, in a context
where a club is known, is a bug.
"""
from typing import Optional

from sqlmodel import Session, select

from models import ClubSystem, SystemConfig

# The fields a club's system admin may override. Deliberately does NOT include
# faction_list / faction_groups / icon_folder (a club does not get its own
# armies for a shared game), name / slug / legacy_system_name (identity, and
# the string every historical Signup row is keyed on), or active (a
# platform-wide kill switch).
OVERRIDABLE_FIELDS = (
    "uses_points",
    "default_points",
    "max_points",
    "uses_scenarios",
    "scenario_options",
    "default_scenario",
    "allows_demo",
    "uses_standby",
    "has_intro_prepass",
)


def _is_set(value) -> bool:
    """Whether a club actually expressed an opinion in this column.

    None is unset. An empty list is also unset, matching the convention the
    vibe override already uses, where [] is how the UI clears one. False is NOT
    unset — "this club does not use points" is a real answer and the whole
    reason the boolean columns are nullable rather than defaulted.
    """
    if value is None:
        return False
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return False
    return True


class EffectiveSystem:
    """A SystemConfig as one club actually runs it.

    Proxies every attribute of the underlying catalogue row, so `config.id`,
    `config.legacy_system_name` and `config.recent_weeks` all keep working, and
    substitutes the club's override wherever one is set.

    Read-only. It is not a SQLModel instance, it is never added to a session,
    and writing to it changes nothing — write to the ClubSystem row instead.
    """

    def __init__(self, config: SystemConfig, club_system: Optional[ClubSystem] = None):
        self._config = config
        self._club_system = club_system
        overrides = {}
        if club_system is not None:
            for field in OVERRIDABLE_FIELDS:
                value = getattr(club_system, field, None)
                if _is_set(value):
                    overrides[field] = value

        # A system cannot ask for a scenario it has no options for. Reachable
        # by a club turning scenarios on and clearing the list, and by a
        # platform system that has the flag set with an empty catalogue list.
        # Better a system with no scenarios than a dropdown with nothing in it
        # and a matcher scoring agreement between two blanks.
        self._overrides = overrides
        if self.uses_scenarios and not (self.scenario_options or []):
            overrides["uses_scenarios"] = False

        # Same for points: uses_points with no ceiling would let a signup
        # through with any number in it, because signups.py clamps against
        # max_points and min() against None raises.
        if self.uses_points and self.max_points is None:
            overrides["max_points"] = self._config.max_points or 10000

    def __getattr__(self, name):
        # Only reached for attributes not found on the instance, so the two
        # underscore-prefixed fields set in __init__ never come through here.
        overrides = self.__dict__.get("_overrides") or {}
        if name in overrides:
            return overrides[name]
        return getattr(self.__dict__["_config"], name)

    @property
    def overridden_fields(self) -> list:
        """Which fields this club has actually set, for the admin UI to show
        what is customised versus inherited."""
        return [f for f in OVERRIDABLE_FIELDS if f in self._overrides]

    def __repr__(self) -> str:
        return (f"<EffectiveSystem {self._config.legacy_system_name!r} "
                f"overrides={sorted(self._overrides)}>")


def effective_system(db: Session, club_id: Optional[int], config: SystemConfig) -> EffectiveSystem:
    """The system as `club_id` runs it. No club, or no ClubSystem row, gives
    the catalogue row untouched — which is what an unscoped public read and a
    club that does not run this system should both see."""
    if config is None:
        return None
    cs = None
    if club_id is not None:
        cs = db.exec(
            select(ClubSystem).where(
                ClubSystem.club_id == club_id,
                ClubSystem.system_id == config.id,
            )
        ).first()
    return EffectiveSystem(config, cs)
