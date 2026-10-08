"""Abstract interfaces for cross-source entity matching and deduplication."""

from abc import ABC, abstractmethod
from dataclasses import dataclass

from contractor_scraper.normalization.base import NormalizedCompanyRecord


@dataclass
class MatchResult:
    """Outcome of matching an incoming entity against existing registry records."""

    matched_company_id: str | None
    is_match: bool
    similarity_score: float
    match_method: str


class BaseCompanyMatcher(ABC):
    """Abstract interface for matching incoming public records against existing database records."""

    @abstractmethod
    def match(
        self,
        candidate: NormalizedCompanyRecord,
        candidates_pool: list[NormalizedCompanyRecord],
    ) -> MatchResult:
        """Evaluate if candidate matches an existing company in the pool."""
        ...


class BaseDeduplicator(ABC):
    """Abstract interface for bulk deduplication of company record batches."""

    @abstractmethod
    def deduplicate(
        self,
        records: list[NormalizedCompanyRecord],
    ) -> list[NormalizedCompanyRecord]:
        """Eliminate duplicate entities from a batch, retaining the highest quality record."""
        ...
