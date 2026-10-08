"""Final verified lead model conforming strictly to the five mandatory output fields."""

import re
from datetime import datetime

from pydantic import Field, field_validator

from contractor_scraper.core.constants import US_STATES
from contractor_scraper.core.enums import ContractorCategory
from contractor_scraper.models.base import BaseSchema, current_utc_time

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class FinalLead(BaseSchema):
    """The clean company lead model designed for practical contractor outreach.

    Fields include:
    - Company Name
    - Website (if found)
    - Email (publicly extracted, blank if none found)
    - City
    - State
    - Formation Date
    - Document Number
    - Company Status
    - Street Address
    - ZIP Code
    - Industry
    """

    company_name: str = Field(
        ..., min_length=1, max_length=255, description="Official company name"
    )
    website: str = Field(
        default="", max_length=500, description="Discovered official company website"
    )
    email: str = Field(
        default="", max_length=255, description="Public business email (blank if not found)"
    )
    city: str = Field(..., min_length=1, max_length=120, description="City location")
    state: str = Field(
        ..., min_length=2, max_length=2, description="2-letter U.S. state postal code"
    )
    formation_date: str = Field(default="", description="Filing or formation date (YYYY-MM-DD)")
    document_number: str = Field(
        default="", description="State corporate entity or document number"
    )
    company_status: str = Field(default="Active", description="Corporate entity status")
    street_address: str = Field(default="", description="Physical street address")
    zip_code: str = Field(default="", description="ZIP code")
    industry: str = Field(default="", description="Contractor trade or industry")

    # Optional metadata
    category: ContractorCategory | None = None
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    verified_at: datetime = Field(default_factory=current_utc_time)

    @field_validator("company_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Company Name cannot be empty")
        return cleaned

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not v:
            return ""
        cleaned = v.strip().lower()
        if not cleaned:
            return ""
        if "@" in cleaned and "." in cleaned.split("@")[-1]:
            return cleaned
        return ""

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        if not v:
            return ""
        cleaned = v.strip()
        if not cleaned:
            return ""
        if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
            cleaned = f"https://{cleaned}"
        return cleaned

    @field_validator("state")
    @classmethod
    def validate_state(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if cleaned not in US_STATES:
            raise ValueError(f"'{v}' is not a valid 2-letter U.S. state postal code")
        return cleaned

    def to_output_dict(self) -> dict[str, str]:
        """Produce dictionary with clean, practical lead generation fields."""
        return {
            "Company Name": self.company_name,
            "Website": self.website,
            "Email": self.email,
            "City": self.city,
            "State": self.state,
            "Formation Date": self.formation_date,
            "Document Number": self.document_number,
            "Company Status": self.company_status,
            "Street Address": self.street_address,
            "ZIP Code": self.zip_code,
            "Industry": self.industry,
        }
