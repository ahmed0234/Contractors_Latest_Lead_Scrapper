"""Domain models for website candidates and verification results."""

from datetime import datetime
from uuid import uuid4

from pydantic import Field, field_validator

from contractor_scraper.core.enums import WebsiteStatus
from contractor_scraper.models.base import BaseSchema, current_utc_time


class WebsiteBase(BaseSchema):
    """Base model for a discovered company website."""

    company_id: str = Field(..., description="Foreign key to associated company")
    url: str = Field(..., min_length=4, max_length=500, description="Website URL")
    domain: str = Field(
        ..., min_length=3, max_length=255, description="Root domain (e.g. example.com)"
    )
    is_verified: bool = Field(
        default=False, description="Whether website has been successfully verified"
    )
    verification_status: WebsiteStatus = Field(default=WebsiteStatus.PENDING)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    discovered_via: str = Field(default="search", max_length=100)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, v: str) -> str:
        stripped = v.strip()
        if not (stripped.startswith("http://") or stripped.startswith("https://")):
            return f"https://{stripped}"
        return stripped


class WebsiteCreate(WebsiteBase):
    """Schema for recording a newly found candidate website."""

    id: str = Field(default_factory=lambda: str(uuid4()))


class WebsiteRead(WebsiteBase):
    """Schema for reading a persisted website record."""

    id: str
    created_at: datetime = Field(default_factory=current_utc_time)
