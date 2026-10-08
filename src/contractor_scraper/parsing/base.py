"""Abstract interfaces for parsing raw public records into structured company entities."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedCompanyRecord:
    """Intermediate structured representation of an entity extracted from raw source data."""

    source_name: str
    source_record_id: str
    raw_name: str
    raw_city: str | None = None
    raw_state: str | None = None
    raw_license_number: str | None = None
    raw_industry_codes: list[str] = field(default_factory=list)
    raw_filing_date: str | None = None
    raw_address: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class BaseParser(ABC):
    """Abstract base class for extracting standardized company records from raw payloads."""

    @abstractmethod
    def parse_payload(
        self, raw_content: Any, metadata: dict[str, Any] | None = None
    ) -> list[ParsedCompanyRecord]:
        """Parse raw response bytes, text, or JSON payload into structured records."""
        ...
