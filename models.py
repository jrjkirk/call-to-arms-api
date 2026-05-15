"""SQLModel definitions, ported from the Streamlit app.

These mirror the schema in Supabase exactly. We don't manage migrations here —
the source of truth for the schema is still the Streamlit app for now. We're
strictly reading from these tables until later in the migration.
"""
from datetime import datetime
from typing import Optional
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


class Signup(SQLModel, table=True):
    """Call to Arms responses per player/week/system."""
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


class LeagueResult(SQLModel, table=True):
    """Submitted Old World League game results."""
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


class LeagueRating(SQLModel, table=True):
    """Current Old World League rating, separate from the shared player profile."""
    __tablename__ = "league_ratings"
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    player_id: int = Field(index=True)
    player_name: str
    rating: float = 1000.0
    updated_at: datetime = Field(default_factory=datetime.utcnow)