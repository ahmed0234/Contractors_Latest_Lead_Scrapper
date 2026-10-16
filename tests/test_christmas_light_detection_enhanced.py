"""Comprehensive test suite verifying enhanced Christmas Light Installation detection requirements."""

import pytest
from contractor_scraper.classification.christmas_light_classifier import (
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.discovery.crawler import CrawledPage, CrawlResult


def test_landscaping_company_with_christmas_lights_qualifies():
    """A landscaping company that explicitly offers Christmas light installation must qualify."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="greenvalleygardens.com",
        base_url="https://greenvalleygardens.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://greenvalleygardens.com",
            status_code=200,
            title="Green Valley Landscaping & Lawn Care",
            text_content=(
                "Green Valley Landscaping provides lawn mowing, mulch installation, landscape design, "
                "and seasonal Christmas light installation for residential and commercial properties. "
                "Call us today for a free estimate."
            ),
        ),
        combined_text=(
            "Green Valley Landscaping provides lawn mowing, mulch installation, landscape design, "
            "and seasonal Christmas light installation for residential and commercial properties. "
            "Call us today for a free estimate."
        ),
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Green Valley Landscaping")
    assert result.classification == "Yes"
    assert result.is_qualified is True
    assert "christmas light installation" in result.evidence_phrases or "multi-trade" in result.reasoning.lower()


def test_electrical_contractor_with_holiday_lights_qualifies():
    """An electrical contractor that installs holiday lights must qualify."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="sparkselectric.com",
        base_url="https://sparkselectric.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://sparkselectric.com",
            status_code=200,
            title="Sparks Electric | Licensed Electrical Contractors",
            text_content=(
                "Licensed electricians offering residential wiring, EV chargers, generators, and "
                "custom holiday lighting installation & removal services. Get a free quote for your "
                "holiday lights setup."
            ),
        ),
        combined_text=(
            "Licensed electricians offering residential wiring, EV chargers, generators, and "
            "custom holiday lighting design and installation & removal services. Get a free quote for your "
            "holiday lights setup."
        ),
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Sparks Electric")
    assert result.classification == "Yes"
    assert result.is_qualified is True
    assert "holiday lighting" in result.reasoning.lower() or "holiday" in "".join(result.evidence_phrases).lower()


def test_ordinary_indoor_lighting_only_excluded():
    """A company that only installs ordinary indoor lights must be excluded."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="apexindoorlighting.com",
        base_url="https://apexindoorlighting.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://apexindoorlighting.com",
            status_code=200,
            title="Apex Electrical & Lighting Fixtures",
            text_content=(
                "We install indoor lighting fixtures, ceiling fans, chandeliers, recessed lighting, "
                "under cabinet lighting, and panel upgrades. Our licensed electricians handle all home "
                "electrical repairs and rewiring. Licensed & insured with 20 years experience."
            ),
        ),
        combined_text=(
            "We install indoor lighting fixtures, ceiling fans, chandeliers, recessed lighting, "
            "under cabinet lighting, and panel upgrades. Our licensed electricians handle all home "
            "electrical repairs and rewiring. Licensed & insured with 20 years experience."
        ),
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Apex Electrical")
    assert result.classification == "No"
    assert result.is_qualified is False
    assert "indoor" in result.reasoning.lower() or "electrical" in result.reasoning.lower()


def test_landscape_lighting_only_excluded():
    """A company that only installs permanent landscape lighting must be excluded."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="luxurylandscapelighting.com",
        base_url="https://luxurylandscapelighting.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://luxurylandscapelighting.com",
            status_code=200,
            title="Luxury Landscape Lighting & Architectural Illumination",
            text_content=(
                "We specialize in permanent low voltage landscape lighting, pathway lighting, "
                "garden lighting, tree uplighting, and outdoor audio systems for estates and fine homes. "
                "Transform your backyard with subtle pathway lights and architectural downlighting."
            ),
        ),
        combined_text=(
            "We specialize in permanent low voltage landscape lighting, pathway lighting, "
            "garden lighting, tree uplighting, and outdoor audio systems for estates and fine homes. "
            "Transform your backyard with subtle pathway lights and architectural downlighting."
        ),
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Luxury Landscape Lighting")
    assert result.classification == "No"
    assert result.is_qualified is False
    assert "landscape" in result.reasoning.lower() or "architectural" in result.reasoning.lower()


def test_exterior_service_provider_with_christmas_lights_qualifies():
    """A company that provides Christmas light installation alongside other exterior services must qualify."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="crystalclearwash.com",
        base_url="https://crystalclearwash.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://crystalclearwash.com",
            status_code=200,
            title="Crystal Clear Window Cleaning & Pressure Washing",
            text_content=(
                "Exterior home maintenance experts: Window cleaning, pressure washing, gutter cleaning, "
                "and professional Christmas light hanging & takedown services. Book a free consultation "
                "for your holiday lighting needs."
            ),
        ),
        combined_text=(
            "Exterior home maintenance experts: Window cleaning, pressure washing, gutter cleaning, "
            "and professional Christmas light hanging & takedown services. Book a free consultation "
            "for your holiday lighting needs."
        ),
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Crystal Clear Wash")
    assert result.classification == "Yes"
    assert result.is_qualified is True
    assert "christmas light hanging" in result.evidence_phrases or "holiday" in result.reasoning.lower()


def test_accessible_website_insufficient_evidence_is_uncertain():
    """An accessible website with no sufficient evidence must be classified as Uncertain rather than automatically treated as Production."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="newdomainplaceholder.com",
        base_url="https://newdomainplaceholder.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://newdomainplaceholder.com",
            status_code=200,
            title="Under Construction",
            text_content="Website under construction. Coming soon in 2026. Contact us for inquiries.",
        ),
        combined_text="Website under construction. Coming soon in 2026. Contact us for inquiries.",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="New Domain Placeholder")
    assert result.classification == "Uncertain"
    assert result.is_qualified is False
    assert "insufficient" in result.reasoning.lower() or "minimal" in result.reasoning.lower()


def test_inaccessible_website_classified_unreachable():
    """A website that failed after recovery attempts must be classified as Unreachable with accurate diagnostic status."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="nonexistentdomain12345.com",
        base_url="https://nonexistentdomain12345.com",
        is_accessible=False,
        error_message="Connection refused on all candidate variations",
        status_category="Connection Error",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Nonexistent Domain")
    assert result.classification == "Unreachable"
    assert result.website_status == "Connection Error"
    assert "Connection refused" in result.access_error


def test_permanent_holiday_gemstone_dealer_qualifies():
    """Authorized Gemstone / Trimlight / Jellyfish permanent holiday lighting dealers must qualify."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="everbrightcolorado.com",
        base_url="https://everbrightcolorado.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://everbrightcolorado.com",
            status_code=200,
            title="EverBright Permanent Holiday Lighting",
            text_content=(
                "Authorized Gemstone Lights installation contractor for residential homes and businesses. "
                "Enjoy invisible permanent Christmas lights year-round. Request a free estimate."
            ),
        ),
        combined_text="Authorized Gemstone Lights installation contractor for residential homes and businesses. permanent christmas lights request a free estimate.",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="EverBright Colorado")
    assert result.classification == "Yes"
    assert result.is_qualified is True
    assert any("gemstone" in p or "permanent" in p for p in result.evidence_phrases)
