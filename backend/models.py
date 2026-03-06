from pydantic import BaseModel, Field
from typing import Optional


class ScrapeRequest(BaseModel):
    token: str = Field(min_length=1)
    guild_id: str = Field(min_length=1, pattern=r"^\d+$")


class UpdateMemberRequest(BaseModel):
    quirks: Optional[list[str]] = Field(default=None, max_length=100)
    notes: Optional[str] = Field(default=None, max_length=10000)


class ValidateTokenRequest(BaseModel):
    token: str = Field(min_length=1)
