"""Domain models for company entities."""

from datetime import datetime
from uuid import uuid4

from pydantic import Field, field_validator

from contractor_scraper.core.constants import US_STATES
from contractor_scraper.core.enums import CompanyStatus, ContractorCategory
from contractor_scraper.models.base import BaseSchema, current_utc_time


class CompanyBase(BaseSchema):
    """Core attributes representing a contractor company."""

    name: str = Field(..., min_length=1, max_length=255, description="Normalized company name")
    raw_name: str | None = Field(None, max_length=255, description="Original unparsed company name")
    city: str = Field(..., min_length=1, max_length=120, description="City location")
    state: str = Field(
        ..., min_length=2, max_length=2, description="2-letter U.S. state postal code"
    )
    category: ContractorCategory = Field(..., description="Target contractor trade category")
    website: str | None = Field(None, max_length=500, description="Verified official website URL")
    email: str | None = Field(None, max_length=255, description="Verified primary business email")
    status: CompanyStatus = Field(
        default=CompanyStatus.DISCOVERED, description="Current lifecycle status"
    )
    confidence_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Overall data confidence"
    )

    @field_validator("state")
    @classmethod
    def validate_state_code(cls, v: str) -> str:
        code = v.strip().upper()
        if code not in US_STATES:
            raise ValueError(f"'{v}' is not a valid 2-letter U.S. postal code")
        return code


class CompanyCreate(CompanyBase):
    """Schema for inserting a newly discovered company."""

    id: str = Field(default_factory=lambda: str(uuid4()))


class CompanyUpdate(BaseSchema):
    """Schema for updating company enrichment status or verified contact info."""

    name: str | None = None
    city: str | None = None
    state: str | None = None
    website: str | None = None
    email: str | None = None
    status: CompanyStatus | None = None
    confidence_score: float | None = Field(None, ge=0.0, le=1.0)


class CompanyRead(CompanyBase):
    """Schema for reading a persisted company record."""

    id: str
    created_at: datetime = Field(default_factory=current_utc_time)
    updated_at: datetime = Field(default_factory=current_utc_time)
