"""Email extraction and verification exports."""

from contractor_scraper.extraction.base import (
    BaseEmailExtractor,
    BaseEmailVerifier,
    EmailCandidate,
    EmailVerificationResult,
)
from contractor_scraper.extraction.email_extractor import EmailExtractor
from contractor_scraper.extraction.email_verifier import EmailVerifier

__all__ = [
    "BaseEmailExtractor",
    "BaseEmailVerifier",
    "EmailCandidate",
    "EmailVerificationResult",
    "EmailExtractor",
    "EmailVerifier",
]
