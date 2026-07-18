"""SQLModel definitions, ported from the Streamlit app.

These mirror the schema in Supabase exactly. We don't manage migrations here —
the source of truth for the schema is still the Streamlit app for now. We're
strictly reading from these tables until later in the migration.
"""
from datetime import date, datetime
from typing import Optional
from sqlalchemy import Column, JSON
from sqlmodel import SQLModel, Field


class Player(SQLModel, table=True):
    __tablename__ = "players"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    default_faction: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True
    titles: Optional[str] = Field(default=None)
    admin_notes: Optional[str] = Field(default=None)
    announced_achievements: Optional[str] = Field(default=None)
    club_id: Optional[int] = Field(default=None, foreign_key="clubs.id", index=True)


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

    # Whether this system runs a league (ELO ladder + results). Replaces the
    # hardcoded `system == "The Old World"` league checks so league
    # eligibility is per-system data, not a name check. Distinct from
    # Club.leagues_enabled, which gates whether a club runs leagues at all.
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
    leagues_enabled: bool = True


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

    webhook_type is one of: signup, pairings, call_to_arms (system_id
    meaningful for these three) or league_result, league_rankings,
    achievement (system_id always None — club-level, not per-system).
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