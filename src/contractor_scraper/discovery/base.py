"""Abstract interfaces for official website discovery and verification."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from contractor_scraper.core.enums import WebsiteStatus


@dataclass
class WebsiteCandidate:
    """Discovered potential company website candidate."""

    url: str
    domain: str
    discovery_source: str
    initial_confidence: float = 0.5


@dataclass
class WebsiteVerificationResult:
    """Outcome of validating whether a website is the genuine active business site."""

    url: str
    domain: str
    status: WebsiteStatus
    is_verified: bool
    confidence: float
    verification_checks: list[str] = field(default_factory=list)
    failure_reason: str | None = None


class BaseWebsiteFinder(ABC):
    """Abstract interface for discovering candidate websites for a business."""

    @abstractmethod
    async def find_websites(
        self,
        company_name: str,
        city: str,
        state: str,
    ) -> list[WebsiteCandidate]:
        """Search public web indices and directories for matching official business websites."""
        ...


class BaseWebsiteVerifier(ABC):
    """Abstract interface for verifying candidate websites.

    Ensures domain is active, not parked, not an aggregate directory,
    and genuinely matches the target business name and geography.
    """

    @abstractmethod
    async def verify_website(
        self,
        candidate: WebsiteCandidate,
        expected_company_name: str,
        expected_state: str,
        expected_city: str | None = None,
    ) -> WebsiteVerificationResult:
        """Deep verification of candidate website."""
        ...
