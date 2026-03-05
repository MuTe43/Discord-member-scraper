from pydantic import BaseModel
from typing import Optional


class ScrapeRequest(BaseModel):
    token: str
    guild_id: str


class UpdateMemberRequest(BaseModel):
    quirks: Optional[list[str]] = None
    notes: Optional[str] = None


class ValidateTokenRequest(BaseModel):
    token: str
