"""Abstract interfaces for contractor trade and industry classification."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from contractor_scraper.core.enums import ContractorCategory


@dataclass
class ClassificationResult:
    """Outcome of evaluating whether an entity genuinely operates in the target contractor category."""

    target_category: ContractorCategory
    is_match: bool
    confidence: float
    matching_keywords: list[str] = field(default_factory=list)
    exclusion_keywords: list[str] = field(default_factory=list)
    reasoning: str | None = None


class BaseIndustryClassifier(ABC):
    """Abstract interface for identifying and verifying contractor industry classification.

    Applies deterministic keyword taxonomies, NAICS/SIC state licensing code mapping,
    and business description verification.
    """

    @abstractmethod
    def classify(
        self,
        company_name: str,
        target_category: ContractorCategory,
        industry_codes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ClassificationResult:
        """Evaluate if the company belongs to the specified trade category."""
        ...

    @abstractmethod
    def verify_business_trade(
        self,
        company_name: str,
        website_content_summary: str,
        target_category: ContractorCategory,
    ) -> ClassificationResult:
        """Deep verification using company website content to confirm genuine active service."""
        ...
