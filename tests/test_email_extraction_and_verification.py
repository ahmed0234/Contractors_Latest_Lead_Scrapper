"""Tests for email candidate extraction, template rejection, and domain verification."""

import asyncio

from contractor_scraper.core.enums import EmailStatus
from contractor_scraper.discovery.crawler import CrawlResult
from contractor_scraper.extraction.base import EmailCandidate
from contractor_scraper.extraction.email_extractor import EmailExtractor
from contractor_scraper.extraction.email_verifier import EmailVerifier


def test_email_candidate_filtering() -> None:
    """Verify EmailExtractor rejects template, dummy, and image-based emails."""
    extractor = EmailExtractor()

    invalid_emails = [
        "example@example.com",
        "user@domain.com",
        "admin@mysite.com",
        "noreply@apexroofing.com",
        "logo.png@apexroofing.com",
        "banner.svg@apexroofing.com",
        "test@test.com",
        "invalid-email-no-at",
    ]
    for e in invalid_emails:
        assert extractor._is_valid_email_candidate(e) is False, f"{e} should have been rejected"

    valid_emails = [
        "info@apexroofingfl.com",
        "sales@apexroofingfl.com",
        "john.doe@apexroofingfl.com",
        "apexroofingfl@gmail.com",
    ]
    for e in valid_emails:
        assert extractor._is_valid_email_candidate(e) is True, f"{e} should be valid"


def test_email_scoring_prioritization() -> None:
    """Verify domain-matched emails receive highest confidence score."""
    extractor = EmailExtractor()

    score_domain_match = extractor._score_email("info@apexroofingfl.com", "apexroofingfl.com")
    score_webmail = extractor._score_email("apexroofing@gmail.com", "apexroofingfl.com")
    score_unrelated = extractor._score_email("contractor@unrelatedsite.org", "apexroofingfl.com")

    assert score_domain_match == 0.95
    assert score_webmail == 0.75
    assert score_unrelated <= 0.30


def test_email_extractor_from_crawl() -> None:
    """Verify extractor extracts and sorts emails from CrawlResult."""
    extractor = EmailExtractor()
    mock_crawl = CrawlResult(
        domain="apexroofingfl.com",
        base_url="https://apexroofingfl.com",
        is_accessible=True,
        all_emails=["contact@apexroofingfl.com", "apexroofing@gmail.com", "admin@domain.com"],
    )

    candidates = asyncio.run(
        extractor.extract_emails("https://apexroofingfl.com", crawl_result=mock_crawl)
    )
    assert len(candidates) == 2  # admin@domain.com filtered
    # First candidate should be domain match
    assert candidates[0].email_address == "contact@apexroofingfl.com"
    assert candidates[0].initial_confidence >= 0.90


def test_email_verifier_accepts_public_business_email() -> None:
    """Verify EmailVerifier marks domain-matched email as verified without network DNS/SMTP pings."""
    verifier = EmailVerifier(check_mx=False)

    cand = EmailCandidate(
        email_address="info@apexroofingfl.com",
        source_url="https://apexroofingfl.com",
        initial_confidence=0.95,
    )

    result = asyncio.run(verifier.verify_email(cand, expected_domain="apexroofingfl.com"))
    assert result.is_verified is True
    assert result.status == EmailStatus.DELIVERABLE
    assert result.confidence >= 0.90
    assert "domain_exact_match" in result.verification_checks


def test_email_verifier_rejects_malformed_email() -> None:
    """Verify EmailVerifier rejects emails with invalid email syntax."""
    verifier = EmailVerifier(check_mx=False)

    cand = EmailCandidate(
        email_address="not-an-email",
        source_url="https://site.com",
        initial_confidence=0.50,
    )

    result = asyncio.run(verifier.verify_email(cand))
    assert result.is_verified is False
    assert result.status == EmailStatus.REJECTED
    assert "Invalid email format" in result.failure_reason
