"""Unit tests for the Christmas and Holiday Light Installation classifier."""

from contractor_scraper.classification.christmas_light_classifier import (
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.discovery.crawler import CrawledPage, CrawlResult


def test_qualified_christmas_light_contractor():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="holidayillumination.com",
        base_url="https://holidayillumination.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://holidayillumination.com",
            status_code=200,
            title="Christmas Light Installation Atlanta GA | Free Estimate",
            meta_description="Professional Christmas and holiday light installation for homes and businesses.",
            text_content=(
                "We provide full-service residential Christmas light installation, commercial holiday lighting, "
                "in-season maintenance, takedown and storage. Request a free estimate from our licensed installers."
            ),
        ),
        combined_text="residential christmas light installation commercial holiday lighting takedown and storage free estimate our installers",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Holiday Illumination")
    assert result.classification == "Yes"
    assert result.decision_label == "Yes"
    assert result.is_qualified is True
    assert result.website_status == "OK"
    assert len(result.evidence_phrases) >= 1
    assert "takedown and storage" in result.evidence_phrases or "residential christmas light" in result.evidence_phrases


def test_permanent_holiday_lighting_trimlight_qualified():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="trimlightatl.com",
        base_url="https://trimlightatl.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://trimlightatl.com",
            status_code=200,
            title="Trimlight Installation & Permanent Holiday Lighting",
            text_content="Authorized Trimlight installer for permanent holiday lighting on homes and businesses. Schedule a consultation for a free quote.",
        ),
        combined_text="authorized trimlight installer permanent holiday lighting permanent christmas lights schedule a consultation free quote",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Trimlight Atlanta")
    assert result.classification == "Yes"
    assert result.is_qualified is True


def test_ecommerce_retail_seller_disqualified():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="holidaylightshop.com",
        base_url="https://holidaylightshop.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://holidaylightshop.com",
            status_code=200,
            title="Buy Christmas Lights Online | Free Shipping",
            text_content=(
                "Add to cart. View cart. Proceed to checkout. Buy online Christmas lights, C9 replacement bulbs, "
                "spools of wire, and clips. Free shipping on orders over $99. Product catalog."
            ),
        ),
        combined_text="add to cart view cart proceed to checkout buy online christmas lights product catalog",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Holiday Light Shop")
    assert result.classification == "No"
    assert result.decision_label == "No"
    assert result.is_qualified is False
    assert "e-commerce" in result.reasoning.lower() or "product seller" in result.reasoning.lower()


def test_unrelated_retail_store_disqualified():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="muji.us",
        base_url="https://www.muji.us",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://www.muji.us",
            status_code=200,
            title="MUJI USA | Home Goods | Apparel | Stationery Store",
            text_content="Official site for MUJI. Shop home decor, furniture, apparel, clothing store, stationery and accessories.",
        ),
        combined_text="muji usa apparel clothing store stationery store home goods shop online",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Muji")
    assert result.classification == "No"
    assert result.is_qualified is False


def test_wholesale_supplier_disqualified():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="southernlightsorlando.com",
        base_url="http://southernlightsorlando.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="http://southernlightsorlando.com",
            status_code=200,
            title="Southern Lights | Wholesale Lighting Source",
            text_content="Since 1991, Southern Lights has been supplying commercial lighting to Central Florida as a wholesale lighting company with experienced salespersons.",
        ),
        combined_text="wholesale lighting company commercial lighting supplier vendors products & services",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Southern Lights")
    assert result.classification == "No"
    assert result.is_qualified is False
    assert "wholesale" in result.reasoning.lower()


def test_unreachable_timeout():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="unreachablesite.com",
        base_url="https://unreachablesite.com",
        is_accessible=False,
        error_message="Domain crawl timed out after 15.0s",
        status_category="Timeout",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Unreachable Contractor")
    assert result.classification == "Unreachable"
    assert result.is_qualified is False
    assert result.website_status == "Timeout"
    assert "timed out" in result.access_error


def test_unreachable_bot_challenge():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="protectedlights.com",
        base_url="https://protectedlights.com",
        is_accessible=False,
        error_message="Access blocked by web firewall/challenge (Attention Required! | Cloudflare)",
        status_category="Bot Challenge",
        status_code=403,
    )

    result = classifier.evaluate_crawl(crawl, company_name="Protected Lights")
    assert result.classification == "Unreachable"
    assert result.website_status == "Bot Challenge"


def test_uncertain_minimal_text():
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="vaguesite.com",
        base_url="https://vaguesite.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://vaguesite.com",
            status_code=200,
            title="Welcome to ABC",
            text_content="Welcome to our business page. Under maintenance. Contact info coming soon.",
        ),
        combined_text="welcome under maintenance contact info coming soon",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="ABC")
    assert result.classification == "Uncertain"
    assert result.decision_label == "Uncertain"
    assert result.is_qualified is False
    assert "insufficient" in result.reasoning.lower()


def test_multitrade_landscaping_with_holiday_decorating_qualified():
    """Ensure a landscaper who also offers holiday decorating/lighting is qualified, not excluded."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="greenlandscapenj.com",
        base_url="https://greenlandscapenj.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://greenlandscapenj.com",
            status_code=200,
            title="Green Landscape & Holiday Decorating | New Jersey",
            meta_description="Lawn care, garden lighting, and full holiday decorating services.",
            text_content=(
                "We provide full lawn care, landscape design, pathway lighting, and garden lighting. "
                "In the winter, our team provides custom holiday decorating, Christmas decor setup and takedown. "
                "Free estimates. Licensed and insured."
            ),
        ),
        combined_text="lawn care landscape design pathway lighting garden lighting holiday decorating christmas decor setup and takedown free estimates licensed and insured",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Green Landscape")
    assert result.classification == "Yes"
    assert result.decision_label == "Yes"
    assert result.is_qualified is True


def test_multitrade_electrician_with_holiday_lighting_qualified():
    """Ensure an electrical contractor who offers holiday lighting is qualified, not excluded as indoor electrical."""
    classifier = ChristmasLightClassifier()
    crawl = CrawlResult(
        domain="brightsparkelectric.com",
        base_url="https://brightsparkelectric.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://brightsparkelectric.com",
            status_code=200,
            title="Bright Spark Electric | Commercial & Residential Electricians",
            meta_description="Recessed lighting, panel upgrades, and seasonal holiday lighting installation.",
            text_content=(
                "Residential electrical contractor offering panel upgrades, ceiling fans, and recessed lighting. "
                "We also offer professional holiday lighting and permanent exterior lighting for homes. "
                "Contact our installation crew for a free quote."
            ),
        ),
        combined_text="panel upgrades ceiling fans recessed lighting holiday lighting permanent exterior lighting free quote",
        status_category="OK",
    )

    result = classifier.evaluate_crawl(crawl, company_name="Bright Spark Electric")
    assert result.classification == "Yes"
    assert result.decision_label == "Yes"
    assert result.is_qualified is True

