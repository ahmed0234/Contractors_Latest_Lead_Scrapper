"""Tests for website crawler content extraction and multi-signal verification."""

import asyncio
from unittest.mock import AsyncMock

from contractor_scraper.core.enums import WebsiteStatus
from contractor_scraper.discovery.base import WebsiteCandidate
from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler
from contractor_scraper.discovery.website_verifier import WebsiteVerifier


def test_crawler_html_text_cleaning() -> None:
    """Verify HTML cleaning extracts title, description, visible text, and links."""
    crawler = WebsiteCrawler()
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Apex Roofing Orlando | Professional Roof Repair</title>
        <meta name="description" content="Leading roofing contractor in Orlando, Florida.">
        <style>body { color: red; }</style>
        <script>console.log("analytics");</script>
    </head>
    <body>
        <h1>Welcome to Apex Roofing</h1>
        <p>Serving Orlando and Central Florida with top quality roof replacement.</p>
        <a href="/contact">Contact Us</a>
        <a href="mailto:info@apexroofing.com">Email Us</a>
    </body>
    </html>
    """

    title, meta, text, links = crawler._clean_text_from_html(sample_html)
    assert "Apex Roofing Orlando" in title
    assert "Leading roofing contractor" in meta
    assert "console.log" not in text
    assert "Serving Orlando" in text
    assert "/contact" in links


def test_crawler_email_and_phone_extraction() -> None:
    """Verify crawler extracts mailto links and text emails while ignoring images."""
    crawler = WebsiteCrawler()
    html = '<a href="mailto:sales@apexroofing.com">Email</a>'
    text = "Call us at (407) 555-1234 or email support@apexroofing.com. Ignore logo.png@apex.com"

    emails, phones = crawler._extract_emails_and_phones(html, text)
    assert "sales@apexroofing.com" in emails
    assert "support@apexroofing.com" in emails
    assert not any(e.startswith("logo.png") for e in emails)
    assert "(407) 555-1234" in phones


def test_cloudflare_email_decoding() -> None:
    """Verify Cloudflare XOR email decoding."""
    from contractor_scraper.discovery.crawler import decode_cloudflare_email

    # "info@contractor.com" XOR with key 0x12:
    # 'i' = 0x69 ^ 0x12 = 0x7b
    # 'n' = 0x6e ^ 0x12 = 0x7c
    # Let's test a known example:
    # encoded: key=12, text="info@test.com"
    email = "info@contractor.com"
    key = 0x2A
    encoded = f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)
    decoded = decode_cloudflare_email(encoded)
    assert decoded == email

    # Test extracting from HTML with data-cfemail
    crawler = WebsiteCrawler()
    html = f'<p>Contact us: <span data-cfemail="{encoded}">[email&#160;protected]</span></p>'
    emails, _ = crawler._extract_emails_and_phones(html, "")
    assert email in emails


def test_crawler_obfuscated_email_extraction() -> None:
    """Verify crawler handles obfuscated emails like info [at] roofingfl.com."""
    crawler = WebsiteCrawler()
    html = "<p>Reach us directly</p>"
    text = "Reach us at contact [at] floridaroof.com for a free estimate"

    emails, _ = crawler._extract_emails_and_phones(html, text)
    assert "contact@floridaroof.com" in emails


def test_website_verifier_positive_match() -> None:
    """Verify candidate website matches when company name, location, and trade align."""
    mock_crawler = WebsiteCrawler()
    mock_crawl = CrawlResult(
        domain="apexroofingfl.com",
        base_url="https://apexroofingfl.com",
        is_accessible=True,
        combined_text="Apex Roofing Contractors Orlando Florida commercial and residential roof repair services.",
    )
    mock_crawler.crawl_domain = AsyncMock(return_value=mock_crawl)

    verifier = WebsiteVerifier(crawler=mock_crawler)
    candidate = WebsiteCandidate(
        url="https://apexroofingfl.com",
        domain="apexroofingfl.com",
        discovery_source="search",
        initial_confidence=0.85,
    )

    result = asyncio.run(
        verifier.verify_website(
            candidate=candidate,
            expected_company_name="Apex Roofing LLC",
            expected_state="FL",
            expected_city="Orlando",
            category="roofing",
        )
    )

    assert result.is_verified is True
    assert result.status == WebsiteStatus.VERIFIED
    assert result.confidence >= 0.75
    assert "state_fl_match" in result.verification_checks
    assert "city_orlando_match" in result.verification_checks


def test_website_verifier_rejects_unrelated_site() -> None:
    """Verify verifier rejects websites lacking name and location signals."""
    mock_crawler = WebsiteCrawler()
    mock_crawl = CrawlResult(
        domain="unrelatedsite.com",
        base_url="https://unrelatedsite.com",
        is_accessible=True,
        combined_text="Welcome to National Digital Marketing Agency New York NY.",
    )
    mock_crawler.crawl_domain = AsyncMock(return_value=mock_crawl)

    verifier = WebsiteVerifier(crawler=mock_crawler)
    candidate = WebsiteCandidate(
        url="https://unrelatedsite.com",
        domain="unrelatedsite.com",
        discovery_source="search",
        initial_confidence=0.3,
    )

    result = asyncio.run(
        verifier.verify_website(
            candidate=candidate,
            expected_company_name="Apex Roofing LLC",
            expected_state="FL",
            expected_city="Orlando",
            category="roofing",
        )
    )

    assert result.is_verified is False
    assert result.status == WebsiteStatus.REJECTED
    assert result.confidence < 0.70


def test_website_verifier_rejects_parked_domain() -> None:
    """Verify verifier identifies and rejects parked/for-sale domain landers."""
    mock_crawler = WebsiteCrawler()
    mock_crawl = CrawlResult(
        domain="apexroofingfl.com",
        base_url="https://apexroofingfl.com",
        is_accessible=True,
        combined_text="Buy this domain! This domain is for sale on GoDaddy.",
    )
    mock_crawler.crawl_domain = AsyncMock(return_value=mock_crawl)

    verifier = WebsiteVerifier(crawler=mock_crawler)
    candidate = WebsiteCandidate(
        url="https://apexroofingfl.com",
        domain="apexroofingfl.com",
        discovery_source="synthetic_probe",
        initial_confidence=0.9,
    )

    result = asyncio.run(
        verifier.verify_website(
            candidate=candidate,
            expected_company_name="Apex Roofing LLC",
            expected_state="FL",
            expected_city="Orlando",
            category="roofing",
        )
    )

    assert result.is_verified is False
    assert result.status == WebsiteStatus.PARKED_DOMAIN
    assert "Parked or for-sale" in (result.failure_reason or "")


def test_website_verifier_matches_street_address_and_zip() -> None:
    """Verify verifier incorporates physical street address and zip code matches."""
    mock_crawler = WebsiteCrawler()
    mock_crawl = CrawlResult(
        domain="apexroofingfl.com",
        base_url="https://apexroofingfl.com",
        is_accessible=True,
        combined_text="Apex Roofing, 1234 Colonial Drive, Orlando FL 32801. Professional roofing contractor.",
    )
    mock_crawler.crawl_domain = AsyncMock(return_value=mock_crawl)

    verifier = WebsiteVerifier(crawler=mock_crawler)
    candidate = WebsiteCandidate(
        url="https://apexroofingfl.com",
        domain="apexroofingfl.com",
        discovery_source="synthetic_probe",
        initial_confidence=0.85,
    )

    result = asyncio.run(
        verifier.verify_website(
            candidate=candidate,
            expected_company_name="Apex Roofing LLC",
            expected_state="FL",
            expected_city="Orlando",
            category="roofing",
            expected_street_address="1234 Colonial Drive",
            expected_zip_code="32801",
        )
    )

    assert result.is_verified is True
    assert "zip_32801_match" in result.verification_checks
    assert "street_address_match" in result.verification_checks
