"""Tests for website candidate discovery, domain parsing, and directory exclusion."""

from pathlib import Path

from contractor_scraper.discovery.website_finder import (
    WebsiteFinder,
    extract_root_domain,
)


def test_extract_root_domain() -> None:
    """Verify domain extraction normalizes and removes www."""
    assert extract_root_domain("https://www.apexroofing.com/about") == "apexroofing.com"
    assert extract_root_domain("http://apexroofing.com") == "apexroofing.com"
    assert extract_root_domain("apexroofing.com/contact") == "apexroofing.com"
    assert extract_root_domain("https://subdomain.roofers.org:8080/page") == "subdomain.roofers.org"


def test_blocked_directory_domains() -> None:
    """Verify directories, aggregators, and social portals are blocked."""
    finder = WebsiteFinder()

    blocked = [
        "yelp.com",
        "m.yelp.com",
        "yellowpages.com",
        "bbb.org",
        "angi.com",
        "homeadvisor.com",
        "facebook.com",
        "instagram.com",
        "sunbiz.org",
        "bizapedia.com",
        "thumbtack.com",
        "houzz.com",
    ]
    for domain in blocked:
        assert finder._is_blocked_domain(domain) is True, f"{domain} should be blocked"

    allowed = [
        "apexroofingfl.com",
        "tampabayroofers.net",
        "orlandoroofrepairs.org",
    ]
    for domain in allowed:
        assert finder._is_blocked_domain(domain) is False, f"{domain} should be allowed"


def test_domain_relevance_scoring() -> None:
    """Verify domains containing company tokens score significantly higher."""
    finder = WebsiteFinder()

    high_score = finder._calculate_domain_relevance("apexroofing.com", "Apex Roofing LLC")
    medium_score = finder._calculate_domain_relevance("apexservices.com", "Apex Roofing LLC")
    low_score = finder._calculate_domain_relevance("floridahomes.com", "Apex Roofing LLC")

    assert high_score > medium_score > low_score
    assert high_score >= 0.80


def test_discovery_caching(tmp_path: Path) -> None:
    """Verify discovery results are saved to and read from disk cache."""
    finder = WebsiteFinder(cache_dir=tmp_path)
    cache_file = finder._get_cache_path("Apex Roofing", "Orlando", "FL")
    assert not cache_file.exists()

    # Pre-populate cache
    cache_file.write_text(
        '[{"url": "https://apexroofing.com", "domain": "apexroofing.com", "discovery_source": "cached", "initial_confidence": 0.95}]',
        encoding="utf-8",
    )

    import asyncio

    candidates = asyncio.run(finder.find_websites("Apex Roofing", "Orlando", "FL"))
    assert len(candidates) == 1
    assert candidates[0].domain == "apexroofing.com"
    assert candidates[0].initial_confidence == 0.95
