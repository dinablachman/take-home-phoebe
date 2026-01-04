"""
Example domain models. Implement or replace as needed.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class Caregiver(BaseModel):
    id: str
    name: str
    role: str
    phone: str


class Shift(BaseModel):
    id: str
    organization_id: str
    role_required: str
    start_time: datetime
    end_time: datetime
    claimed: bool = False  # True when shift is assigned to caregiver
    claimed_by: str | None = None  # Caregiver ID
    claimed_at: datetime | None = None
    fanout_started_at: datetime | None = None
    declined_caregiver_ids: list[str] = Field(default_factory=list)
