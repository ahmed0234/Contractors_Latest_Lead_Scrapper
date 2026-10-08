"""Domain models for extracted email addresses and verification states."""

import re
from datetime import datetime
from uuid import uuid4

from pydantic import Field, field_validator

from contractor_scraper.core.enums import EmailStatus
from contractor_scraper.models.base import BaseSchema, current_utc_time

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class EmailBase(BaseSchema):
    """Base attributes for an extracted company email."""

    company_id: str = Field(..., description="Foreign key to company")
    email_address: str = Field(..., max_length=255, description="Extracted email address")
    is_verified: bool = Field(
        default=False, description="Whether email has been verified as deliverable"
    )
    verification_status: EmailStatus = Field(default=EmailStatus.PENDING)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_url: str | None = Field(
        None, max_length=500, description="Page URL where email was discovered"
    )

    @field_validator("email_address")
    @classmethod
    def validate_and_lower_email(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not EMAIL_REGEX.match(cleaned):
            raise ValueError(f"'{v}' is not a syntactically valid email address")
        return cleaned


class EmailCreate(EmailBase):
    """Schema for recording a newly extracted email."""

    id: str = Field(default_factory=lambda: str(uuid4()))


class EmailRead(EmailBase):
    """Schema for reading a persisted email record."""

    id: str
    created_at: datetime = Field(default_factory=current_utc_time)
