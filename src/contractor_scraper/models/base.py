"""Base Pydantic configuration and common schemas."""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class BaseSchema(BaseModel):
    """Base schema providing standard configuration across all Pydantic models."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=True,
    )


def current_utc_time() -> datetime:
    """Helper to return standardized timezone-aware UTC datetime."""
    return datetime.now(UTC)
