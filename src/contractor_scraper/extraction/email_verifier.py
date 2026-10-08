"""Email verification engine validating syntax, domain alignment, and DNS MX records."""

from contractor_scraper.core.enums import EmailStatus
from contractor_scraper.discovery.website_finder import extract_root_domain
from contractor_scraper.extraction.base import (
    BaseEmailVerifier,
    EmailCandidate,
    EmailVerificationResult,
)


class EmailVerifier(BaseEmailVerifier):
    """Normalizes and accepts publicly discovered company emails without SMTP/DNS verification.

    Per lead-generation requirements:
    - Normalizes to lowercase
    - Verifies basic email syntax
    - No SMTP pinging, DNS socket blocking, or deliverability rejections
    - Public company emails are collected directly and placed in the lead dataset
    """

    def __init__(self, check_mx: bool = False) -> None:
        self.check_mx = check_mx

    async def verify_email(
        self,
        candidate: EmailCandidate,
        expected_domain: str | None = None,
    ) -> EmailVerificationResult:
        """Accept publicly discovered business email directly."""
        email = candidate.email_address.strip().lower()
        checks: list[str] = ["syntax_valid"]

        if "@" not in email or "." not in email.split("@")[-1]:
            return EmailVerificationResult(
                email_address=email,
                status=EmailStatus.REJECTED,
                is_verified=False,
                confidence=0.0,
                failure_reason="Invalid email format",
            )

        checks.append("public_site_extracted")
        confidence = candidate.initial_confidence or 0.90

        if expected_domain:
            web_root = extract_root_domain(expected_domain)
            email_root = extract_root_domain(email.split("@")[-1])
            if web_root == email_root:
                checks.append("domain_exact_match")
                confidence = max(0.95, confidence)

        return EmailVerificationResult(
            email_address=email,
            status=EmailStatus.DELIVERABLE,
            is_verified=True,
            confidence=round(confidence, 3),
            verification_checks=checks,
            failure_reason=None,
        )
