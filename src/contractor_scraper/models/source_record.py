"""Domain models for raw public data source records (e.g. state registries, filings)."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field

from contractor_scraper.models.base import BaseSchema, current_utc_time


class SourceRecordBase(BaseSchema):
    """Base fields for an acquired public entity record."""

    company_id: str | None = Field(None, description="Associated company ID once matched")
    source_name: str = Field(
        ..., max_length=100, description="Registry name (e.g. sos_texas, cslb_california)"
    )
    source_record_id: str = Field(
        ..., max_length=150, description="Unique identifier within public source"
    )
    raw_payload: dict[str, Any] = Field(
        default_factory=dict, description="Raw unparsed record payload"
    )
    processed_status: str = Field(default="UNPROCESSED", max_length=50)


class SourceRecordCreate(SourceRecordBase):
    """Schema for inserting an acquired source record."""

    id: str = Field(default_factory=lambda: str(uuid4()))


class SourceRecordRead(SourceRecordBase):
    """Schema for reading a source record."""

    id: str
    acquired_at: datetime = Field(default_factory=current_utc_time)
