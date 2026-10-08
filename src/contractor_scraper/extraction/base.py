"""Abstract interfaces for official business email extraction and verification."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from contractor_scraper.core.enums import EmailStatus


@dataclass
class EmailCandidate:
    """Discovered business email candidate."""

    email_address: str
    source_url: str
    context: str | None = None
    initial_confidence: float = 0.5


@dataclass
class EmailVerificationResult:
    """Outcome of validating syntax, MX records, deliverability, and domain alignment."""

    email_address: str
    status: EmailStatus
    is_verified: bool
    confidence: float
    verification_checks: list[str] = field(default_factory=list)
    failure_reason: str | None = None


class BaseEmailExtractor(ABC):
    """Abstract interface for extracting candidate business emails from company web pages."""

    @abstractmethod
    async def extract_emails(self, website_url: str) -> list[EmailCandidate]:
        """Extract email candidates from homepage, /contact, /about, and footer pages."""
        ...


class BaseEmailVerifier(ABC):
    """Abstract interface for verifying candidate business email deliverability.

    Performs syntax validation, MX DNS record verification, disposable domain checks,
    and domain-alignment checks without sending spam or relying on paid commercial APIs.
    """

    @abstractmethod
    async def verify_email(
        self,
        candidate: EmailCandidate,
        expected_domain: str | None = None,
    ) -> EmailVerificationResult:
        """Verify deliverability and genuineness of the email candidate."""
        ...
