"""Abstract base normalizer interface and normalized record structures."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from contractor_scraper.normalization.cleaners import (
    clean_city_name,
    clean_company_name,
    clean_state_code,
)
from contractor_scraper.parsing.base import ParsedCompanyRecord


@dataclass
class NormalizedCompanyRecord:
    """Canonical cleaned entity representation ready for matching and classification."""

    source_name: str
    source_record_id: str
    normalized_name: str
    raw_name: str
    city: str
    state: str
    street_address: str = ""
    zip_code: str = ""
    license_number: str | None = None
    industry_codes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseNormalizer(ABC):
    """Abstract normalizer contract for standardizing parsed company records."""

    @abstractmethod
    def normalize(self, record: ParsedCompanyRecord) -> NormalizedCompanyRecord | None:
        """Normalize a parsed record into canonical fields, or return None if invalid."""
        ...


class DefaultCompanyNormalizer(BaseNormalizer):
    """Standard baseline normalizer implementation."""

    def normalize(self, record: ParsedCompanyRecord) -> NormalizedCompanyRecord | None:
        clean_name = clean_company_name(record.raw_name)
        if not clean_name:
            return None

        clean_state = clean_state_code(record.raw_state or "")
        clean_city = clean_city_name(record.raw_city or "")

        if not clean_state or not clean_city:
            return None

        meta = record.raw_metadata or {}
        street_address = (
            record.raw_address
            or meta.get("street_address")
            or meta.get("address1")
            or ""
        ).strip()
        zip_code = (
            meta.get("zip_code")
            or meta.get("zip")
            or ""
        ).strip()

        return NormalizedCompanyRecord(
            source_name=record.source_name,
            source_record_id=record.source_record_id,
            normalized_name=clean_name,
            raw_name=record.raw_name,
            city=clean_city,
            state=clean_state,
            street_address=street_address,
            zip_code=zip_code,
            license_number=record.raw_license_number,
            industry_codes=record.raw_industry_codes,
            metadata=record.raw_metadata,
        )
