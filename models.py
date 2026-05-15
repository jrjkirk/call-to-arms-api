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
    titles: Optional[str] = Field(default=None)  # JSON list, e.g. ["Champion of the Empire 2026"]
    admin_notes: Optional[str] = Field(default=None)  # private notes for admins
    announced_achievements: Optional[str] = Field(default=None)  # JSON list of achievements already announced