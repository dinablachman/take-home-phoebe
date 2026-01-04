"""
Example domain models. Implement or replace as needed.
"""

from datetime import datetime

from pydantic import BaseModel


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
    claimed_by: str | None = None  # Caregiver.id
    claimed_at: datetime | None = None  # record time of shift assignment
    fanout_started_at: datetime | None = None  # record time of shift fanout
