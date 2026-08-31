"""SQLModel definitions, ported from the Streamlit app.

These mirror the schema in Supabase exactly. We don't manage migrations here —
the source of truth for the schema is still the Streamlit app for now. We're
strictly reading from these tables until later in the migration.
"""
from datetime import date, datetime
from typing import Optional
from sqlalchemy import Column, JSON, UniqueConstraint
from sqlmodel import SQLModel, Field


# The 12 ONS ITL1 UK regions — the controlled vocabulary for Club.region, used
# to group clubs under headers in the discovery dropdown. Kept in this exact
# order (roughly N→S, then the devolved nations) so the dropdown reads sensibly.
# Mirrored in the frontend (call-to-arms-web src/lib/regions.ts) — keep in sync.
UK_REGIONS: list[str] = [
    "North East",
    "North West",
    "Yorkshire & the Humber",
    "East Midlands",
    "West Midlands",
    "East of England",
    "London",
    "South East",
    "South West",
    "Scotland",
    "Wales",
    "Northern Ireland",
]


class Player(SQLModel, table=True):
    __tablename__ = "players"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    default_faction: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Roster membership, NOT identity. False = archived: the player has left,
    # so they're hidden from admin pickers, signup and the league, but the row
    # and all its history stay put and the owning account STAYS LINKED.
    #
    # This used to gate identity too — active_player_id_for filtered on it —
    # which meant archiving someone made the app believe their Discord account
    # had no profile at all, offer them "create a profile", and hand them a
    # second Player row with an empty history. Two real players hit that. See
    # the note on active_player_id_for in database.py.
    active: bool = True
    # Separate from `active`: a player who is still very much on the roster but
    # doesn't want to appear in league rankings — a casual, or someone who asked
    # to be left out. They still sign up and get paired as normal. Archiving
    # implies this (an archived player is out of the league either way), so the
    # league filters on both.
    league_visible: bool = True
    titles: Optional[str] = Field(default=None)
    admin_notes: Optional[str] = Field(default=None)
    announced_achievements: Optional[str] = Field(default=None)
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)
    # Multi-club "network" model (2026-07-25): a Discord account owns one Player
    # per club it plays at, so ownership lives HERE (Player.user_id) rather than
    # on User.player_id (which capped a user at one player, one club). NULL =
    # an unclaimed roster entry a club pre-seeded. During the expand phase both
    # links coexist and are kept consistent; User.player_id stays the source of
    # truth for the user's *home*-club player until the call-site sweep lands.
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    # Discord-guild gate (2026-08-02): stamped the first time this player is
    # confirmed to be a member of their club's Discord server, and never
    # cleared. Acts as the cache that keeps the gate to ONE Discord API call
    # per player for their entire life — every later signup just reads this
    # column. NULL means "not yet checked", not "not a member".
    discord_verified_at: Optional[datetime] = Field(default=None)


class Signup(SQLModel, table=True):
    __tablename__ = "signups"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    week: str
    system: str

    player_id: Optional[int] = Field(default=None, index=True)
    player_name: str

    faction: Optional[str] = None
    points: Optional[int] = None
    eta: Optional[str] = None
    experience: Optional[str] = None
    vibe: Optional[str] = None
    standby_ok: bool = False
    tnt_ok: bool = False
    scenario: Optional[str] = None
    can_demo: bool = False
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class Pairing(SQLModel, table=True):
    __tablename__ = "pairings"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)

    week: str
    system: str

    a_signup_id: int
    b_signup_id: Optional[int] = None

    status: str = "pending"
    table: Optional[str] = None

    a_faction: Optional[str] = None
    b_faction: Optional[str] = None

    prearranged: bool = Field(default=False)
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class PublishState(SQLModel, table=True):
    __tablename__ = "publish_state"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    week: str
    system: str
    published: bool = False
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class PairingBlock(SQLModel, table=True):
    __tablename__ = "pairing_blocks"

    id: Optional[int] = Field(default=None, primary_key=True)
    player_a_id: int
    player_b_id: int
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Phase 1 expand/contract step, table 1 of 10. Nullable during
    # backfill/dual-run; a later contract step makes this NOT NULL once
    # every row is populated. See multitenancy-plan-v2.md.
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    key: str = Field(primary_key=True)
    value: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClubSetting(SQLModel, table=True):
    """Per-club settings (composite PK), split out of app_settings — see
    multitenancy-plan-v2.md. app_settings stays global-only (e.g.
    systems_from_catalogue); auto_pairings_* keys live here instead."""
    __tablename__ = "club_settings"

    club_id: int = Field(foreign_key="clubs.id", primary_key=True)
    key: str = Field(primary_key=True)
    value: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LeagueResult(SQLModel, table=True):
    __tablename__ = "league_results"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    player_1_id: Optional[int] = Field(default=None, index=True)
    player_1_name: str
    player_2_id: Optional[int] = Field(default=None, index=True)
    player_2_name: str
    result: str
    result_date: str

    player_1_faction: Optional[str] = None
    player_2_faction: Optional[str] = None
    player_1_painting_bonus: Optional[str] = None
    player_2_painting_bonus: Optional[str] = None
    game_type: str = "Competitive"

    player_1_rating_before: Optional[float] = None
    player_2_rating_before: Optional[float] = None
    player_1_rating_after: Optional[float] = None
    player_2_rating_after: Optional[float] = None
    k_factor_used: Optional[int] = None
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)
    # Per-(club, system) modular leagues. system_id ties a result to one
    # system's league; season_id ties it to a season (ratings reset each
    # season). Both nullable during the expand migration, backfilled to The
    # Old World + the initial season for existing rows, then made NOT NULL.
    system_id: Optional[int] = Field(default=None, foreign_key="systems.id", index=True)
    season_id: Optional[int] = Field(default=None, foreign_key="league_seasons.id", index=True)


class LeagueRating(SQLModel, table=True):
    __tablename__ = "league_ratings"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(index=True)
    player_name: str
    rating: float = 1000.0
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)
    # Ratings are per (player, club, system, season) — the recalc rebuilds one
    # season's ratings at a time. Nullable during expand, backfilled, NOT NULL.
    system_id: Optional[int] = Field(default=None, foreign_key="systems.id", index=True)
    season_id: Optional[int] = Field(default=None, foreign_key="league_seasons.id", index=True)


class LeagueSeason(SQLModel, table=True):
    """A dated season for one club's league in one system. Ratings reset each
    season (the recalc keys on season_id); past seasons stay archived. The
    "current" season is the one whose [start_date, end_date] contains today
    (end_date NULL = open-ended / ongoing)."""
    __tablename__ = "league_seasons"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)
    name: str
    start_date: date
    end_date: Optional[date] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LeagueConfig(SQLModel, table=True):
    """Scoring configuration for one club's league in one system. One row per
    (club_id, system_id). Defaults reproduce the original hardcoded ELO
    exactly (K 10 casual / 40 competitive, painting +3/+1, start 1000)."""
    __tablename__ = "league_configs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)

    # "elo" | "winloss"  ("bayesian" reserved for later)
    scoring_method: str = "elo"

    # ELO params
    starting_rating: float = 1000.0
    k_casual: int = 10
    k_competitive: int = 40
    painting_fully_bonus: float = 3.0
    painting_partial_bonus: float = 1.0

    # Flat win/loss points
    points_win: float = 3.0
    points_draw: float = 1.0
    points_loss: float = 0.0
    # Whether the win/loss method also adds the painting bonuses above.
    winloss_use_painting: bool = False

class PairingConfig(SQLModel, table=True):
    """Pairing weighting configuration for one club's system. One row per
    (club_id, system_id). Weights combine the soft matchmaking factors
    (mirror faction, rematch history, vibe, experience, eta, scenario,
    points) into a single score for ranking candidate opponents — see
    pairings_engine._pair_dist(). Defaults approximate the original
    lexicographic priority order (mirror > rematch > vibe > experience >
    eta > scenario > points) but are not a byte-exact reproduction of it.
    `last_opp_pen` / `block_pen` are NOT configurable here — those stay
    hard, unconfigurable top-priority filters in the engine."""
    __tablename__ = "pairing_configs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)

    # 0-10 scale (arbitrary magnitude units — only the ratios between them
    # matter, not the absolute range). Defaults approximate the original
    # priority order at 1/10th the earlier 0-100-scale values.
    weight_mirror: float = 5.0
    weight_rematch: float = 3.0
    weight_vibe: float = 1.5
    weight_experience: float = 0.8
    weight_eta: float = 0.4
    weight_scenario: float = 0.2
    weight_points: float = 0.1

    # Per-club override of the rematch lookback windows (weeks). NULL = fall
    # back to the platform SystemConfig.recent_weeks / extended_weeks default.
    # recent = hard-avoid rematch window; extended = soft-avoid window. These
    # sit alongside the weights because "how far back counts as a rematch" is
    # part of the same per-(club,system) pairing taste as "how much a rematch
    # matters".
    recent_weeks: Optional[int] = None
    extended_weeks: Optional[int] = None


class User(SQLModel, table=True):
    """An authenticated user. Links a Discord identity to a player_id (after claim)."""
    __tablename__ = "users"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    discord_id: str = Field(unique=True, index=True)
    discord_name: str
    avatar_url: Optional[str] = None
    player_id: Optional[int] = Field(default=None, index=True)
    is_super_admin: bool = Field(default=False)
    is_platform_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login_at: datetime = Field(default_factory=datetime.utcnow)
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)
    # Multi-club network model (2026-07-25): the club a user lands on by
    # default. "Soft" — it grants nothing (playing is open to every club), it
    # just picks the default active club when no subdomain says otherwise.
    # Backfilled from club_id. club_id itself is retained during the expand
    # phase and stays authoritative until the active-club resolver sweep lands.
    home_club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class AdminRole(SQLModel, table=True):
    """Grants a user admin access for a specific scope (system or League)."""
    __tablename__ = "admin_roles"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    scope: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class SystemConfig(SQLModel, table=True):
    """Phase 0 systems-as-data catalogue.

    Additive table — created before any code reads it (expand/contract step 1).
    Not on the live write path yet; the hardcoded constants in signups.py /
    pairings_engine.py / render_pairings_image.py remain the source of truth
    until the `systems_from_catalogue` flag (app_settings) is flipped per
    system in a later step.

    `slug` is the new short, human-editable identifier (tow/hh/kt) for this
    catalogue and future code. It is NOT what's stored in
    Signup.system / Pairing.system / PublishState.system today — those
    columns hold the full display string ("The Old World", etc.).
    `legacy_system_name` carries that exact string so catalogue-driven code
    can still join/filter against the existing columns without a data
    migration. `name` is the display name shown in UI, distinct in purpose
    from `legacy_system_name` even though the values coincide today.
    """
    __tablename__ = "systems"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)

    name: str
    slug: str = Field(unique=True, index=True)
    legacy_system_name: str = Field(unique=True, index=True)

    uses_points: bool = False
    default_points: Optional[int] = None
    max_points: Optional[int] = None

    vibe_options: list = Field(default_factory=list, sa_column=Column(JSON))
    default_vibe: Optional[str] = None

    uses_scenarios: bool = False
    scenario_options: Optional[list] = Field(default=None, sa_column=Column(JSON))
    default_scenario: Optional[str] = None

    allows_demo: bool = False
    has_intro_prepass: bool = False

    # Whether this system offers players a "happy to be a standby / sit out if
    # numbers are odd" option on the signup form. Was hardcoded to The Old
    # World only; now a per-system catalogue capability (backfilled TOW=true).
    uses_standby: bool = False

    # Platform-wide catalogue default: does this system generally support a
    # league. Distinct from the real per-club answer, ClubSystem.league_enabled
    # (whether THIS club actually runs one) — main.py's _system_dict prefers
    # the per-club value whenever club context is available; this field is
    # only the fallback for the fully-unscoped GET /systems call.
    has_league: bool = False

    # Pairing-history lookback windows (weeks). HH runs fortnightly so its
    # windows are roughly double TOW/KT's weekly cadence — see
    # pairings_engine.generate(): recent_w, extended_w = (6, 12) for HH,
    # (3, 6) otherwise.
    recent_weeks: int = 3
    extended_weeks: int = 6

    faction_list: Optional[list] = Field(default=None, sa_column=Column(JSON))

    # Informational only for now — render_pairings_image.py currently
    # searches icons/TOW, icons/HH, and icons/KT for every faction lookup
    # regardless of system, so this field does not yet gate anything.
    icon_folder: Optional[str] = None

    active: bool = True


class Club(SQLModel, table=True):
    __tablename__ = "clubs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True
    timezone: str = "Europe/London"
    contact_email: Optional[str] = None
    # leagues_enabled retired 2026-07-19: was a club-wide flag, superseded by
    # ClubSystem.league_enabled (per-system) once leagues went modular. The DB
    # column is left in place (unused, harmless) rather than dropped — same
    # "orphan column over risky migration" call as SystemConfig's old
    # escalation_priority column.

    # Club landing page (Phase: club page feature, 2026-07-20). Managed by
    # the club super-admin. blurb/opening_hours/website/discord are freeform;
    # logo_path/logo_url follow the same Supabase-Storage pattern as
    # Mission.image_path/image_url (path kept so the file can be deleted
    # alongside a re-upload, url denormalized for direct serving).
    blurb: Optional[str] = None
    logo_path: Optional[str] = None
    logo_url: Optional[str] = None
    website_url: Optional[str] = None
    discord_url: Optional[str] = None
    # [{"day": "Monday", "open": "18:00", "close": "22:00", "note": None}, ...]
    opening_hours: Optional[list] = Field(default=None, sa_column=Column(JSON))

    # Location (2026-07-20 follow-up). address is freeform display text;
    # latitude/longitude are entered manually by the super-admin (no free
    # geocoding API available) and drive the OpenStreetMap/Leaflet pin on
    # this club's own Club page and its marker on the multi-club map on the
    # logged-out hero. Either can be set independently — a club with an
    # address but no coordinates just shows the text + a directions link,
    # no map.
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # UK region (2026-07-25, multi-club network). One of UK_REGIONS (the 12 ONS
    # ITL1 regions), set by the club super-admin. Groups this club under a
    # region header in the discovery dropdown. Nullable — a club with no region
    # set just sorts under an "Other" heading rather than being hidden.
    region: Optional[str] = None

    # Discord-guild gate (2026-08-02). The club's Discord server ("guild")
    # snowflake id. NULL = the gate is OFF for this club, whatever the mode
    # setting says — so the feature is inert until a club deliberately sets
    # this. Usually auto-derived from discord_url (an invite code resolves to
    # its guild), with a manual admin field as the fallback for clubs whose
    # Discord is run by someone outside the app's admin team.
    # NOT a secret: a guild id grants nothing on its own — the bot can only
    # read a guild it has been separately invited into. Stored as text
    # because snowflakes exceed a 32-bit int and are only ever compared, not
    # arithmetic'd.
    discord_guild_id: Optional[str] = None


class ClubSystem(SQLModel, table=True):
    """Which systems a club runs, and that club's schedule for each —
    doesn't touch SystemConfig itself (that stays platform-managed and
    shared across all clubs)."""
    __tablename__ = "club_systems"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)
    enabled: bool = True
    session_day: str  # e.g. "Wednesday", "Friday"
    session_cadence: str  # "weekly" | "fortnightly"
    cadence_anchor: Optional[date] = None  # only meaningful when fortnightly
    # Default session start time ("HH:MM", 24h), shown on the Club page
    # calendar's auto-derived session entries (e.g. "The Old World session
    # 18:00"). Optional — a system with no start time set just shows as an
    # all-day entry, same as before this field existed.
    session_start_time: Optional[str] = None

    # Per-club vibe configuration. NULL = fall back to this system's
    # SystemConfig.vibe_options/default_vibe (platform default). Set by a
    # club's own system admin via the club Edit-system form; chosen from the
    # canonical vibe palette (signups.CANONICAL_VIBES) so special-meaning
    # vibes can't be mistyped.
    vibe_options: Optional[list] = Field(default=None, sa_column=Column(JSON))
    default_vibe: Optional[str] = None

    # Per-club-system random mission pool (see the Mission table below).
    # missions_enabled off => the Call-to-Arms post keeps its pre-catalogue
    # behavior (hardcoded SCENARIO_DATA fallback in call_to_arms_content.py).
    # missions_use_secondary gates whether this system's missions carry a
    # "secondary objectives" field (some systems have no equivalent).
    missions_enabled: bool = False
    missions_use_secondary: bool = False

    # Whether this club runs a league for this system (per-system, replacing
    # the old club-wide Club.leagues_enabled gate). Scoring config lives in
    # LeagueConfig, seasons in LeagueSeason.
    league_enabled: bool = False

    # Club landing page systems carousel (2026-07-20). Managed by this
    # system's own admin (_require_system_scope), same ownership model as
    # missions_enabled/missions_use_secondary above. photo_path/photo_url
    # follow the same Supabase-Storage pattern as Mission's image fields —
    # optional, a carousel card can be text-only. accent_color threads this
    # system's identity through the carousel card, calendar entries, and
    # opening-hours grid; NULL falls back to the platform gold accent.
    carousel_blurb: Optional[str] = None
    carousel_photo_path: Optional[str] = None
    carousel_photo_url: Optional[str] = None
    accent_color: Optional[str] = None
    carousel_order: int = 0

    # Whether this club sends venue table-booking emails for this system.
    # Scoring/venue config lives in TableBookingConfig, same ownership model
    # as missions_enabled/league_enabled above.
    table_booking_enabled: bool = False

    # Per-(club, system) Discord server, for the membership gate (2026-08-20).
    # One club can run each of its game nights out of a DIFFERENT Discord
    # server — at EGNWGC, Kill Team and The Old World are separate servers
    # neither of which is a "club" server. So the guild, the invite link and
    # the enforcement mode all belong here, not on Club.
    #
    # All three are NULL-means-inherit: a club whose systems share one Discord
    # sets nothing here and keeps falling back to Club.discord_guild_id /
    # Club.discord_url / the club-level discord_gate_mode setting. That's what
    # keeps the original club-wide gate working untouched.
    #
    # discord_url is also the "Join our Discord" CTA on this system's Club-page
    # carousel card — the invite a player is shown must be the server they're
    # actually being gated on, or the gate sends them to the wrong place.
    discord_guild_id: Optional[str] = None
    discord_url: Optional[str] = None

    # The gate is OPT-IN PER SYSTEM, same shape as missions_enabled /
    # league_enabled / table_booking_enabled above and owned by the same
    # admin. Not every game night wants the friction — one system can require
    # Discord membership while its neighbour at the same club doesn't care,
    # and that's a decision per game night, not per club.
    #
    # False (the default, and the state every existing row migrates into)
    # means this system is never gated, whatever the club-level settings say.
    # There is deliberately NO inheritance here: an opt-in that a club-wide
    # switch could silently turn on for you isn't an opt-in.
    #
    # discord_gate_mode is the rollout dial WITHIN an opted-in system:
    # 'monitor' (log who would be blocked, block nobody) or 'enforce'. NULL
    # resolves to 'monitor', so opting in starts by watching and escalating to
    # enforce stays a deliberate second action.
    discord_gate_enabled: bool = False
    discord_gate_mode: Optional[str] = None


class PlayerExperienceAdjustment(SQLModel, table=True):
    """Games a player says they've played elsewhere, per (club, system).

    Added to the count derived from pairings rather than replacing it, so the
    club's own tally keeps rising underneath and the total can't go stale. It
    also means the adjustment can't be used to hide experience — you can only
    ever add.

    Per system for the same reason the count is: "20 games of Kill Team before
    I joined" says nothing about your Old World experience. `system` holds the
    legacy display string that Signup.system and Pairing.system store, so this
    joins against them without a lookup.
    """
    __tablename__ = "player_experience_adjustments"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    player_id: int = Field(foreign_key="players.id", index=True)
    system: str = Field(index=True)
    extra_games: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PlayerLevelAnnouncement(SQLModel, table=True):
    """The highest level we've already announced for a (club, player, system).

    Levels themselves are derived from pairings and need no storage — this
    table exists only so the same "ding" isn't posted twice, and so switching
    the feature on doesn't fire hundreds of announcements for levels players
    reached months ago. The backfill seeds it to everyone's current level.
    """
    __tablename__ = "player_level_announcements"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    player_id: int = Field(foreign_key="players.id", index=True)
    system: str = Field(index=True)
    last_level: int = 1
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PlayerDiscordVerification(SQLModel, table=True):
    """Per-(player, guild) proof that a player is in a given Discord server.

    Replaces Player.discord_verified_at as the gate's cache once a club can
    have more than one Discord server. That single column could only answer
    "has this player been checked", which silently became the wrong question:
    being in the Kill Team server says nothing about being in the Old World
    one, so one boolean would let a verified KT player through TOW's gate.

    Keyed on guild_id rather than system_id deliberately — the fact being
    cached is "this person is in THAT server", which stays true no matter how
    many systems point at it. Two systems sharing a server therefore share the
    verification, and re-pointing a system at a different server correctly
    invalidates nothing (the old rows just stop being consulted).

    Rows are only ever inserted, never cleared: same one-call-per-player-ever
    guarantee as the column it replaces. A player who later leaves the Discord
    keeps their access — deliberate, matching the original design's bias
    toward never blocking an established member. Uniqueness on
    (player_id, guild_id) is enforced by the check-then-insert in
    signups.require_discord_member, same convention as ClubSystem/ClubWebhook.
    """
    __tablename__ = "player_discord_verifications"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(foreign_key="players.id", index=True)
    guild_id: str = Field(index=True)
    verified_at: datetime = Field(default_factory=datetime.utcnow)


class ClubEvent(SQLModel, table=True):
    """A calendar entry for one club: either a one-off event or an override/
    addition alongside the auto-derived recurring sessions (which come from
    ClubSystem.session_day/session_cadence/cadence_anchor, not stored here).

    system_id is nullable — a club-wide event (e.g. "Christmas closure") has
    no system and is super-admin-only to create; a system_id set means a
    per-system event (e.g. a one-off tournament) manageable by that system's
    own admin, same ownership model as Mission/carousel fields above."""
    __tablename__ = "club_events"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: Optional[int] = Field(default=None, foreign_key="systems.id", index=True)
    title: str
    description: Optional[str] = None
    event_date: date
    start_time: Optional[str] = None  # "HH:MM", None = all_day
    end_time: Optional[str] = None
    all_day: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Mission(SQLModel, table=True):
    """A single random-rotation mission for one club running one system.

    Per-(club_id, system_id) resource, same shape/ownership model as
    ClubWebhook. Curated by that club's per-system admin via the admin panel;
    the weekly Call-to-Arms post picks one active mission at random and uses
    its image_url as the Discord embed image plus name/secondary_objectives as
    message tokens. image_path is the Supabase Storage object path (kept so
    the image can be deleted with the row); image_url is its public URL,
    denormalized for serving/rendering."""
    __tablename__ = "missions"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)
    name: Optional[str] = None
    secondary_objectives: Optional[str] = None
    # image_path/image_url are nullable so a mission can be text-only (e.g.
    # The Old World's "Open Battle", which has no terrain image). The admin
    # upload endpoint still requires an image for new missions; NULLs come
    # from the TOW seed / future text-only support.
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TableBookingConfig(SQLModel, table=True):
    """Venue table-booking email configuration for one club running one
    system. One row per (club_id, system_id), same shape/ownership model as
    LeagueConfig — created/updated by that system's own admin.

    send_mode is "on_publish" (fire when pairings are published, whether by
    the admin panel or the scheduled auto-pairings run) or "cutoff" (fire at
    a fixed day/time regardless of pairing state, using headcount only —
    cutoff_day/cutoff_time only meaningful in this mode). cutoff_time is
    "HH:MM" 24h, resolved against Europe/London by the cutoff scheduler
    script, not server UTC."""
    __tablename__ = "table_booking_configs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)

    venue_name: Optional[str] = None
    venue_email: str
    cc_emails: Optional[list] = Field(default=None, sa_column=Column(JSON))
    players_per_table: int = 2
    include_player_names: bool = True

    send_mode: str = "on_publish"  # "on_publish" | "cutoff"
    cutoff_day: Optional[str] = None   # e.g. "Wednesday", only used in cutoff mode
    cutoff_time: Optional[str] = None  # "HH:MM", only used in cutoff mode

    subject_template: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class TableBookingNotification(SQLModel, table=True):
    """Audit trail + idempotency guard for venue table-booking emails: one
    row per (club_id, system_id, week) that has actually been sent, so the
    same week can never trigger a duplicate send regardless of which
    trigger (on_publish vs cutoff) or how many times a publish/cutoff check
    runs. week matches the "DD/MM/YYYY" string convention used elsewhere
    (see signups.py::_validate_week), not an ISO week number."""
    __tablename__ = "table_booking_notifications"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: int = Field(foreign_key="systems.id", index=True)
    week: str
    tables: int
    headcount: int
    status: str = "sent"  # "sent" | "failed"
    error: Optional[str] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)


class ClubWebhook(SQLModel, table=True):
    """Per-club Discord webhook URLs — Phase 3 step 1, see multitenancy-plan-v2.md.

    Expand-only: seeded from the six existing call sites' env vars by
    seed_club_webhooks.py, but nothing reads from this table yet — every
    call site keeps reading its env var until a later slice switches it
    over. No DB-level unique constraint on (club_id, webhook_type,
    system_id): Postgres treats NULL as distinct per-row, which would
    silently fail to enforce "one row" for the three club-level types
    below where system_id is always NULL (the same trap app_settings had
    before the club_settings split). Uniqueness is enforced purely by the
    seed/write logic's check-then-upsert, same as ClubSystem.

    webhook_type scoping, kept in sync with admin.py's three tuples (which are
    the authority — read them, not this):

      signup, pairings, call_to_arms, level_up   per system, always offered
      league_result, league_rankings, achievement
                                                 per system, offered once that
          system's league is on. These WERE club-level (system_id NULL) until
          leagues became per-system, because a club running two leagues could
          not route their posts to different channels. Existing rows were
          migrated in migrations/split_league_webhooks_per_system.py.
      venue_booking                              club-level, system_id NULL.
          The bar has one staff channel, not one per game night.
    """
    __tablename__ = "club_webhooks"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    webhook_type: str
    system_id: Optional[int] = Field(default=None, foreign_key="systems.id", index=True)
    url: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Platform admin tools (2026-07-20): site banner, scheduled-job health check,
# audit log, cross-club lookup. All platform-admin-only surfaces — see admin.py.
# ---------------------------------------------------------------------------

class PlatformBanner(SQLModel, table=True):
    """Single-row (id always 1) site-wide announcement banner, set by a
    platform admin and shown to every visitor (logged in or not, any club)
    at the top of the app. Not club-owned — this is platform-level, unlike
    everything else in this file scoped by club_id."""
    __tablename__ = "platform_banner"
    __table_args__ = {"extend_existing": True}

    id: int = Field(default=1, primary_key=True)
    message: str
    # "info" | "warning" | "critical" — drives the banner's colour treatment.
    severity: str = "info"
    active: bool = False
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ScheduledJobRun(SQLModel, table=True):
    """One row per invocation of a scheduled GitHub Actions job
    (run_auto_pairings_check.py, run_call_to_arms_check.py). Lets platform
    admin see "is the cron actually running" instead of only finding out a
    job silently stopped when a club complains — see record_job_run() in
    database.py, called once per script run regardless of whether any
    individual club/system inside that run succeeded or errored."""
    __tablename__ = "scheduled_job_runs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    job_name: str = Field(index=True)
    ran_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    # "ok" | "error" — "ok" means the script completed; per-club/system
    # errors inside a run are caught individually and folded into `detail`
    # rather than failing the whole run, so "ok" with a non-empty detail
    # can still mean "completed, but N clubs had errors".
    status: str = "ok"
    detail: Optional[str] = None


class ScheduledJobClaim(SQLModel, table=True):
    """One row per (job, tick) actually worked, so exactly one runner does it.

    The scheduler moved into the API process (see scheduler.py), which makes
    "how many of me are there?" a real question. One machine runs today, but
    `fly deploy` briefly overlaps the outgoing and incoming processes, and the
    day a second machine exists every post would go out twice.

    The unique constraint IS the lock: a tick inserts its claim and only does
    the work if the insert took. Postgres advisory locks were the alternative
    and are wrong here — the session-scoped ones are unreliable through a
    transaction pooler, and the transaction-scoped ones would be released by
    the first db.commit() inside each job's own per-club loop.

    A crashed winner leaves its period claimed and nothing runs until the next
    tick. That is survivable only because the fire windows in week_logic.py run
    to the end of the configured day; the two mechanisms cover each other, so
    don't narrow those windows again without revisiting this.
    """
    __tablename__ = "scheduled_job_claims"
    __table_args__ = (
        UniqueConstraint("job_name", "period_key", name="uq_job_claim_period"),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    job_name: str = Field(index=True)
    # The tick bucket in UTC, e.g. "2026-08-31T12:05". A string rather than a
    # timestamp so the uniqueness is exactly the bucket, with no chance of two
    # runners rounding a datetime a microsecond apart and both winning.
    period_key: str
    claimed_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class AuditLogEntry(SQLModel, table=True):
    """Platform-wide record of notable admin mutations (club create/edit/
    activate, super-admin grant/revoke, scope grant/revoke, system catalogue
    changes) — "who changed X, and when". actor_user_id/actor_name are
    denormalized so the log stays readable even if the acting user is later
    deleted. Not itself club-scoped — a platform admin views the whole
    platform's history in one place; club_id (when the action was about a
    specific club) is carried in target_id/detail instead."""
    __tablename__ = "audit_log_entries"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    actor_user_id: Optional[int] = Field(default=None, index=True)
    actor_name: str
    action: str
    target_type: Optional[str] = None
    target_id: Optional[int] = None
    detail: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ClubRequest(SQLModel, table=True):
    """A "please add my club" submission from the logged-out hero page.
    Not itself a Club row — approving a request is a platform-admin
    decision recorded here; the actual club still gets created by hand via
    the existing POST /admin/platform/clubs flow, since Joel emails the
    requester a getting-started pack before/alongside setting them up.
    reviewed_by_name is denormalized (see AuditLogEntry's actor_name) so
    the record stays readable if the reviewing user is later deleted."""
    __tablename__ = "club_requests"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    status: str = Field(default="pending", index=True)  # "pending" | "approved" | "denied"
    requester_name: str
    requester_email: str
    club_name: str
    club_location: str
    notes: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    reviewed_by_user_id: Optional[int] = None
    reviewed_by_name: Optional[str] = None
    # Set when a platform admin provisions the request into a real Club (one
    # click: create the club from this request + mark it approved). NULL =
    # approved/denied/pending but no club created yet. Prevents double-provision.
    provisioned_club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


class CallOut(SQLModel, table=True):
    """An ad-hoc "call to arms": a player who can't make regular club night
    posts a standing, open request for a game at a specific place/date/time,
    so it doesn't get buried in Discord chat. Per (club, system) — only that
    club's players see it and only that system's Discord channel is notified
    (system matches the legacy name string on Signup.system etc.).

    Lifecycle (status): "open" until someone takes it up ("taken") or its
    game_at passes ("expired"); the creator can "cancel" it. A daily reminder
    is re-posted to Discord while it stays open (see
    scripts/run_call_outs_check.py), throttled by last_reminder_at."""
    __tablename__ = "call_outs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system: str = Field(index=True)

    creator_player_id: int = Field(foreign_key="players.id", index=True)
    creator_name: str

    # UK-local (Europe/London) naive datetime of the proposed game. Doubles as
    # the expiry point: once "now" passes game_at the call-out auto-expires.
    game_at: datetime

    vibe: Optional[str] = None       # game type / experience sought
    faction: Optional[str] = None    # army being brought
    points: Optional[int] = None
    notes: Optional[str] = None

    # "open" | "taken" | "expired" | "cancelled"
    status: str = Field(default="open", index=True)
    taker_player_id: Optional[int] = Field(default=None, foreign_key="players.id")
    taker_name: Optional[str] = None
    taken_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Throttles the once-a-day Discord reminder. Seeded to creation time on
    # insert so the first reminder lands ~24h after the initial post, not an
    # hour later on the next cron tick.
    last_reminder_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)

# ---------------------------------------------------------------------------
# Venue management (2026-08-24)
#
# The club IS the venue — there is no separate Venue entity, and the public
# booking page lives on the club's own landing page. That was a deliberate
# choice over a standalone Venue row: every club already carries the venue
# facts (address, lat/lng, opening_hours, logo, blurb), and a second entity
# would have duplicated all of them for the sake of a slug.
#
# Note the direction here versus TableBookingConfig above. That feature points
# OUTWARD — the club emails a venue saying "book us six tables on Wednesday".
# These tables point INWARD — the public books space at the club's own venue.
# They share a vocabulary and nothing else; don't merge them.
# ---------------------------------------------------------------------------


class VenueConfig(SQLModel, table=True):
    """Booking policy for one club's venue. One row per club, created the
    first time a super-admin opens Venue Admin.

    Separate from Club rather than more columns on it: this is ~15 settings
    that only matter to clubs selling table space, and Club is already the
    widest table in the schema. Same call as LeagueConfig and
    TableBookingConfig, both of which hang their settings off the club rather
    than living in it.
    """
    __tablename__ = "venue_configs"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True, unique=True)

    # Master switch. Off = no public booking page, no endpoints, nothing shown
    # on the club page. Every club starts off; this is also the natural paywall
    # switch if venue management becomes a paid tier.
    enabled: bool = False

    # "instant"  — a booking that fits is confirmed on the spot and staff are
    #              told after the fact.
    # "request"  — every booking lands as `requested` and waits for staff.
    # Configurable rather than fixed: the whole complaint is staff losing time
    # to booking admin, but a busy Saturday may still want a human gate.
    confirm_mode: str = "instant"
    # Whether people without an account can book at all, and on what terms.
    # Guests default to "request" even where members book instantly: an account
    # used to be the abuse control, and with it gone a human looking at each
    # booking from a stranger is what replaces it. A venue that would rather
    # take the bookings and sort it out later can set this to "instant".
    guest_bookings: bool = True
    guest_confirm_mode: str = "request"
    require_phone: bool = True

    # Booking grid. slot_minutes is the granularity start times snap to;
    # min/max duration are what a booker may ask for. Wargames run long, hence
    # a default max of four hours rather than a restaurant's ninety minutes.
    slot_minutes: int = 30
    min_duration_minutes: int = 60
    max_duration_minutes: int = 240

    # How far ahead bookings open, and how close to the start they still take.
    # lead_time_minutes stops someone booking a table for four minutes' time
    # and catching staff cold.
    max_advance_days: int = 60
    lead_time_minutes: int = 60

    max_party_size: int = 8
    # Bookings a single account may hold at once, counting only future ones.
    # The abuse control that replaces "anonymous booking needs a rate limit" —
    # booking requires a Discord login, so the account is the limit.
    max_active_bookings_per_user: int = 3

    # Bookable hours per weekday, independent of Club.opening_hours: a venue is
    # often open before it will take table bookings. Same shape as
    # Club.opening_hours so the admin UI can reuse the editor.
    # [{"day": "Monday", "open": "18:00", "close": "23:00", "closed": false}, ...]
    booking_hours: Optional[list] = Field(default=None, sa_column=Column(JSON))

    # How staff hear about a booking. Both may be on; both may be off (the
    # console still shows everything). Which channel a venue wants is a
    # property of how that venue is run -- a bar with a staff Discord wants a
    # ping, a game store with an inbox wants email -- so it is configuration,
    # not a hardcoded choice.
    notify_email: bool = True
    # Falls back to Club.contact_email when blank, so a venue that has already
    # told us where to write doesn't have to say it twice.
    notify_emails: Optional[list] = Field(default=None, sa_column=Column(JSON))
    notify_discord: bool = False

    # Shown on the public booking page: house rules, parking, "food served
    # until 9". Freeform, venue's own words.
    booking_blurb: Optional[str] = None
    # Appended to the confirmation email/page. Door codes, where to collect
    # terrain, who to ask for.
    confirmation_note: Optional[str] = None

    # Whether the confirmation should mention the club nights running at this
    # venue. The cross-sell -- a bar booking on a Wednesday hears about the
    # Old World night that already runs that evening.
    promote_club_nights: bool = True

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VenueTable(SQLModel, table=True):
    """One bookable table at a club's venue.

    Real rows rather than a bare count, because staff think in named tables:
    "Table 3 is the 6x4 by the window, and it's out of service tonight". A
    count can't express a mixed inventory, can't take one table out, and can't
    tell a booker which table is theirs.
    """
    __tablename__ = "venue_tables"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)

    name: str                                   # "Table 3", "The Snug"
    size_label: Optional[str] = None            # "6x4", "4x4" — display only
    seats: int = 2                              # players it comfortably takes
    # Off = exists but not bookable (broken leg, reserved for staff). Kept
    # rather than deleted so historical bookings still name a real table.
    active: bool = True
    sort_order: int = 0
    notes: Optional[str] = None                 # staff-facing only

    # ---- Where it physically is (the floor plan) -------------------------
    #
    # A venue selling table space is selling a ROOM, and a list of names can't
    # answer "is the far corner free" or "are those two next to each other".
    # These put each table somewhere real, so the same plan serves as the
    # editor, the booking picture and the view of tonight.
    #
    # UNITS ARE FEET throughout, because that is the only unit a wargaming
    # venue thinks in — tables are 6x4, rooms are "about thirty by twenty".
    # Storing centimetres and converting would mean rounding a 6x4 into
    # 182.88cm and back, and showing someone 5.99ft.
    # A palette TOKEN, not a hex value: "slate" | "blue" | "green" | "amber" |
    # "red" | "purple" | "teal" | "grey". Venues colour-code their room —
    # "the blue bank is the tournament tables", "red ones are demo" — and a
    # free colour picker would let someone choose something illegible on a dark
    # plan, or a green that reads as "free" in the Tonight view. Naming the
    # colours keeps the palette restyleable in one place.
    color: str = "slate"
    # "rect" | "round" | "oval". Venues really do have all three — a 6x4
    # gaming table, a 4ft round in the bar, a long oval for a demo game — and a
    # plan that draws them all as rectangles is a plan staff have to translate.
    # width_ft/depth_ft stay the bounding box whatever the shape, so sizing,
    # rotation and the overlap check need no special cases.
    shape: str = "rect"
    room_id: Optional[int] = Field(default=None, foreign_key="venue_rooms.id", index=True)
    # CENTRE position, not a corner: rotation is then a single SVG transform
    # about (pos_x, pos_y) with no offset arithmetic, and a table stays put
    # when it's turned.
    pos_x: Optional[float] = None
    pos_y: Optional[float] = None
    width_ft: float = 6.0
    depth_ft: float = 4.0
    rotation: float = 0.0                       # degrees clockwise

    created_at: datetime = Field(default_factory=datetime.utcnow)


class VenueBooking(SQLModel, table=True):
    """A table booked at a club's venue, by a member or by a guest.

    Times are local "HH:MM" strings against the club's timezone, matching the
    convention already used by ClubEvent.start_time and
    TableBookingConfig.cutoff_time. booking_date is a real date rather than the
    "DD/MM/YYYY" week string used by Signup -- a booking is a specific day, not
    a club week.

    player_id is denormalised alongside user_id because the two answer
    different questions: user_id is who booked (their account, for their "my
    bookings" list), player_id is who they are at this club (for the roster and
    for anything that wants to join bookings to club history). A user with no
    player row at this club can still book.
    """
    __tablename__ = "venue_bookings"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    table_id: int = Field(foreign_key="venue_tables.id", index=True)

    booking_date: date = Field(index=True)
    start_time: str                             # "HH:MM" local
    end_time: str                               # "HH:MM" local, exclusive

    party_size: int = 2
    # What they're playing, when it's a system this club runs. NULL covers
    # "something else" / "not saying" — a venue takes bookings for games it
    # has never heard of, and the drop-down must not be a wall.
    system_id: Optional[int] = Field(default=None, foreign_key="systems.id", index=True)
    game_note: Optional[str] = None             # free text when system_id is NULL

    # NULL for a guest booking: a venue sells tables to the public, and most of
    # that public has no reason to hold a Discord account. What identifies a
    # guest is contact_email plus manage_token, not a row in users.
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    player_id: Optional[int] = Field(default=None, foreign_key="players.id", index=True)
    contact_name: str
    # Required for a guest (it is the only way to reach them, and the only
    # handle abuse limits can count against); still optional for a member,
    # whose account is the contact route.
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    notes: Optional[str] = None                 # booker's own message to staff

    # Secret in the guest's confirmation email that lets them see and cancel
    # this one booking without an account. Every booking gets one so the cancel
    # link in an email works the same whoever booked. Unguessable and scoped to
    # a single row, so leaking it costs one booking rather than an account.
    manage_token: Optional[str] = Field(default=None, index=True)

    # "requested" (awaiting staff, only in confirm_mode="request")
    # "confirmed" | "cancelled" | "no_show"
    status: str = Field(default="confirmed", index=True)
    staff_note: Optional[str] = None
    # Set when staff add a booking themselves for a walk-in or a phone call,
    # so the console can tell "they booked" from "we booked them in".
    created_by_staff: bool = False

    # Set when this row is one table of a venue event (see VenueEvent). An
    # event holds its tables as ordinary bookings on purpose: clash detection,
    # availability and the day view then need no knowledge of events at all.
    event_id: Optional[int] = Field(default=None, foreign_key="venue_events.id", index=True)

    cancelled_at: Optional[datetime] = None
    cancelled_by_user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VenueStaff(SQLModel, table=True):
    """Grants a user the Venue Admin tab at one club.

    Its own grant rather than an admin_roles scope on purpose. Scopes are game
    systems -- valid_scopes() is built from the SystemConfig catalogue, and the
    codebase already retired its one pseudo-scope ("League") when leagues went
    per-system. Venue work is not a game system: the person running the bar
    needs the bookings console and nothing else, and should not have to be
    handed The Old World's admin rights to get it.

    Super-admins and platform admins have venue access implicitly and need no
    row here (see venue.can_admin_venue).
    """
    __tablename__ = "venue_staff"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VenueClubNight(SQLModel, table=True):
    """One club night this venue hosts, and its table plan.

    Covers BOTH kinds of night, because a venue's diary doesn't care whose
    software runs the game:

      system_id set   -- a night Call to Arms runs. Its schedule lives on
          ClubSystem (session_day/cadence/anchor) and is read from there, so
          the venue never re-enters it and the two can't drift apart. Signups
          and pairings exist, so the table plan can be checked against reality.
      system_id NULL  -- a night the venue hosts that this app has nothing to
          do with, and may never: Magic, Bolt Action, Warmachine. Nobody signs
          up here and no pairings are ever generated, so `name`, `session_day`,
          `session_cadence`, `cadence_anchor` and `start_time` are entered by
          venue staff and are the only record of it.

    One table rather than two, so "what runs here on a Wednesday" is one query.
    The alternative — a separate external-nights table — would have meant every
    caller unioning two sources and getting it subtly wrong in one of them.

    Owned by venue staff, not by the system's admin, which is why none of this
    is a column on ClubSystem: the bar's capacity planning and the game night's
    configuration answer to different people.

    expected_tables is a forecast, and a forecast nobody checks is a guess with
    better posture. venue.table_review() holds it against published pairings —
    for a venue-only night there are none, so it reports the plan and says so
    rather than inventing a comparison.
    """
    __tablename__ = "venue_club_nights"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    system_id: Optional[int] = Field(default=None, foreign_key="systems.id", index=True)

    # Venue-only nights: the whole record of the night. Ignored when system_id
    # is set, where SystemConfig and ClubSystem are the source of truth.
    name: Optional[str] = None
    session_day: Optional[str] = None            # "Wednesday"
    session_cadence: Optional[str] = None        # "weekly" | "fortnightly" | "monthly"
    cadence_anchor: Optional[date] = None        # only meaningful when fortnightly
    start_time: Optional[str] = None             # "HH:MM"

    # Palette token, same set as VenueTable.color. A venue running four game
    # nights wants to see WHICH one has the far corner on a Wednesday, and one
    # shade of gold for "held" can't say that.
    color: str = "amber"

    # None = no plan set; the busyness view falls back to estimating from
    # signups, and for a venue-only night to the tables held for it.
    expected_tables: Optional[int] = None
    notes: Optional[str] = None
    active: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VenueNightTable(SQLModel, table=True):
    """Which of the venue's tables suit a club night — and which are held back
    for it.

    One relationship, two strengths, because they're the same fact at different
    volume. "Table 3 is a 6x4, so it suits The Old World" and "Table 3 is The
    Old World's on a Wednesday" both describe a table belonging to a night;
    modelling them as separate features would mean two admin screens, two
    lookups and a way for them to disagree.

        reserved = False -- PREFERRED. Offered first to someone booking that
            game, and shown to them as recommended. No effect on anyone else.
        reserved = True  -- HELD. Preferred, and additionally not offered to the
            public at all on the nights that game meets.

    Keyed on the NIGHT rather than the game system, so a venue-only night
    (Magic, Bolt Action) can hold tables exactly like a Call to Arms one — it
    has no system id to key on.

    A row per (night, table), not a list of ids on the night: a table can belong
    to several nights, since each has its own day — table 3 can be The Old
    World's on Wednesday and Magic's on Thursday without those two ever
    colliding. It also means deleting a table can't strand an id inside
    somebody's JSON array.

    Reservations bind the PUBLIC only. The staff console will still seat a
    walk-in on a held table, because someone standing in a half-empty room can
    see what the rule can't.
    """
    __tablename__ = "venue_night_tables"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    club_night_id: int = Field(foreign_key="venue_club_nights.id", index=True)
    table_id: int = Field(foreign_key="venue_tables.id", index=True)
    reserved: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VenueEvent(SQLModel, table=True):
    """Something the venue is running that takes tables out of circulation: a
    tournament, a launch night, a birthday.

    The tables it holds are ordinary VenueBooking rows carrying this event's id.
    That was a deliberate choice over a second kind of thing that blocks a
    table: every rule about whether a table is free already lives in
    free_tables_for, and an event that blocked tables its own way would be a
    second implementation to keep in step — the exact split that let a table be
    double-booked in every booking system that has ever had one.

    So an event is a booking the venue makes across several tables at once. It
    clashes properly, shows in the diary, and releases its tables the moment
    it's cancelled, all without another line of blocking logic.
    """
    __tablename__ = "venue_events"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)

    name: str
    description: Optional[str] = None
    event_date: date = Field(index=True)
    start_time: str                              # "HH:MM" local
    end_time: str                                # "HH:MM" local, exclusive
    tables_needed: int = 1

    # Whether bookers see it on the booking page's day strip. A tournament is
    # worth advertising; "carpet cleaning" is not, and a venue shouldn't have to
    # choose between blocking the room and telling the public why.
    public: bool = True

    # "pending" | "approved" | "rejected".
    #
    # An event takes the room out of circulation for a whole evening, which is a
    # bigger commitment than any single booking, so it needs a yes from someone
    # who owns the venue rather than someone who works a shift. Same line as
    # VenueStaff: the bar manager runs the diary, the club super-admin decides
    # what the venue commits to.
    #
    # A PENDING EVENT STILL HOLDS ITS TABLES. That matches how a booking request
    # behaves — an unanswered request keeps its slot — and the alternative is
    # worse: a tournament that loses its room while waiting on an answer is a
    # tournament that gets cancelled. Rejecting releases them immediately.
    status: str = Field(default="pending", index=True)
    rejection_reason: Optional[str] = None
    approved_by_user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    approved_at: Optional[datetime] = None

    created_by_user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class VenueRoom(SQLModel, table=True):
    """A room in the venue, and the canvas its tables are laid out on.

    Venues are rarely one open space — a main hall, a back room, a mezzanine —
    and staff talk about them by name ("put them in the back room"). Modelling
    rooms rather than one big canvas also keeps each plan legible: a 60ft hall
    and a 12ft snug drawn to the same scale on one page would waste most of it.

    Dimensions are in FEET, matching VenueTable (see the note there).
    """
    __tablename__ = "venue_rooms"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)

    name: str = "Main room"
    width_ft: float = 30.0
    depth_ft: float = 20.0
    sort_order: int = 0
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VenueFeature(SQLModel, table=True):
    """Something in a room that isn't a table: the bar, a pillar, the door,
    the terrain shelves.

    Not bookable and not counted in capacity — it exists so the plan looks like
    the actual room. That matters more than it sounds: a floor plan a member of
    staff can't recognise at a glance is one they won't trust, and "the table by
    the door" only means something if the door is drawn.
    """
    __tablename__ = "venue_features"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    room_id: int = Field(foreign_key="venue_rooms.id", index=True)

    label: Optional[str] = None
    # Same palette token as VenueTable.color. Walls, rooms and doors ignore it:
    # they're structure, and a plan where the walls are teal stops reading as a
    # building.
    color: str = "grey"
    # "enclosure" | "wall" | "note" | "bar" | "door" | "pillar" | "shelves" |
    # "stairs" | "toilets"
    #
    # `note` is annotation — "Shop", "Staff only" — drawn as text with no box,
    # because a venue's plan has areas that aren't objects.
    #
    # `enclosure` is a room drawn as a BOX — four walls with a hollow middle —
    # rather than four separate wall segments. Venues are made of rooms, not of
    # line segments, and asking someone to assemble a back room out of four
    # rectangles they have to line up by hand is the slowest possible way to
    # describe something they could draw in one drag.
    kind: str = "wall"
    shape: str = "rect"
    pos_x: float = 0.0
    pos_y: float = 0.0
    width_ft: float = 4.0
    depth_ft: float = 2.0
    rotation: float = 0.0

    # Mirrors, about the object's own centre.
    #
    # Only doors have a handedness, and they have TWO independent ones: which
    # jamb it's hinged on, and which side it opens toward. Rotation can't
    # separate them — turning a door 180 degrees swaps both at once — so a
    # left-hand door that opens inward is unreachable from a right-hand one by
    # rotation alone. These are the missing axis. Every other shape is
    # symmetrical and ignores them.
    flip_h: bool = False
    flip_v: bool = False

    created_at: datetime = Field(default_factory=datetime.utcnow)


class VenueSeating(SQLModel, table=True):
    """One club night's table plan for ONE DATE, once its pairings exist.

    VenueClubNight says what a night needs in general — "The Old World wants
    about six tables, and these ten are held for it". This says what it needs
    on the 12th, which is a different and much better-informed question: the
    pairings are out, thirteen people turned up, that's six games and one bye.
    Six tables, not ten.

    Separate from VenueClubNight because it is per-date and disposable. The
    standing plan is the venue's policy; this is one evening's arithmetic, and
    re-running it must never edit the policy.

    THE VENUE SIDE ONLY. Players are told who they're playing by the pairings
    post; the table they end up on is a room-management detail that would just
    be one more thing to get wrong in a Discord message. Nothing here reaches
    the pairings page, the pairings post or any notification — deliberately.

    `released` is the surplus decision, and it is a decision rather than a
    calculation: handing four held tables back to the public is a promise the
    venue can't quietly take back if a late pairing turns up, so a human makes
    it. Until then the tables stay held and merely LOOK spare.
    """
    __tablename__ = "venue_seatings"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    club_night_id: int = Field(foreign_key="venue_club_nights.id", index=True)
    on_date: date = Field(index=True)

    # The pairing key this was built from — "12/08/2026" and the legacy system
    # name, the pair every pairing row is filed under. Stored rather than
    # recomputed so a plan built on Tuesday can still say what it was built
    # from on Wednesday, when the week has moved on.
    week: str
    system: Optional[str] = None

    # Games needing a table when this was generated. A bye needs no table and
    # is not counted; the count is the honest answer to "how many tables does
    # tonight actually need".
    tables_needed: int = 0

    # Surplus handed back to the public. See the class docstring.
    released: bool = False

    generated_at: datetime = Field(default_factory=datetime.utcnow)


class VenueSeat(SQLModel, table=True):
    """One pairing at one table, on one date.

    A row per pairing rather than a list of ids on the seating, because staff
    move ONE game — "put Dave on the big table, he's brought a siege" — and a
    list can't record that without rewriting the lot.

    `locked` marks a seat a human placed by hand. Regenerating the plan (a late
    signup, someone drops out) reshuffles everything else around it and leaves
    a locked seat exactly where it was put. Without this, auto-assign would
    quietly undo every manual fix the moment anything changed, which is the
    fastest way to make staff stop trusting it.
    """
    __tablename__ = "venue_seats"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    club_id: int = Field(foreign_key="clubs.id", index=True)
    seating_id: int = Field(foreign_key="venue_seatings.id", index=True)

    # No FK to pairings: pairings are regenerated wholesale by the admin tools,
    # and a seat pointing at a deleted pairing should be ignorable rather than
    # a constraint violation on somebody else's screen.
    pairing_id: int = Field(index=True)
    table_id: int = Field(foreign_key="venue_tables.id", index=True)
    locked: bool = False
