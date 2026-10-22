"""Precision regression tests: only real Christmas / holiday / permanent / roofline lighting installers qualify.

Many cases mirror false positives seen in production runs (blog safety tips, decor retailers, soffit
lighting, run-together navigation menus, event companies, metadata-only blocked sites).
"""

import asyncio
import csv
import json
from pathlib import Path

from contractor_scraper.classification.christmas_light_classifier import (
    CLASSIFIER_VERSION,
    ChristmasLightClassifier,
)
from contractor_scraper.discovery.crawler import (
    CrawledPage,
    CrawlerConfig,
    CrawlResult,
    WebsiteCrawler,
)
from contractor_scraper.orchestration.christmas_light_enricher import (
    ENRICHMENT_HEADERS,
    ChristmasLightLeadEnricher,
    christmas_crawler_config,
)


def _crawl(domain: str, pages: list[tuple[str, str, str]]) -> CrawlResult:
    home_url, home_title, home_text = pages[0]
    return CrawlResult(
        domain=domain,
        base_url=f"https://{domain}",
        is_accessible=True,
        homepage=CrawledPage(url=home_url, status_code=200, title=home_title, text_content=home_text),
        subpages=[CrawledPage(url=u, status_code=200, title=t, text_content=x) for u, t, x in pages[1:]],
        combined_text=" ".join(f"{t} {x}" for _, t, x in pages),
        pages_fetched=len(pages),
        status_category="OK",
    )


def _classify(domain: str, pages: list[tuple[str, str, str]], company: str = ""):
    return ChristmasLightClassifier().evaluate_crawl(_crawl(domain, pages), company_name=company)


ELECTRICIAN_HOME = (
    "Licensed electrician serving the North Shore. Panel upgrades, rewiring, EV charger installation, "
    "recessed lighting and generators. Licensed and insured. Free estimate. "
)


# --------------------------------------------------------------------------- target niches qualify


def test_dedicated_christmas_light_installer_qualifies() -> None:
    result = _classify(
        "twinklepros-test.com",
        [(
            "https://twinklepros-test.com",
            "Christmas Light Installation in Westfield, MA | Twinkle Pros",
            "We design, install, maintain, take down and store your Christmas lights. Free estimate.",
        )],
    )
    assert result.qualification_status == "Qualified"
    assert result.christmas_service == "Yes"
    assert "Christmas Light Installation" in result.qualified_niches


def test_holiday_lighting_installer_qualifies() -> None:
    result = _classify(
        "brightseason-test.com",
        [(
            "https://brightseason-test.com",
            "Bright Season Holiday Lighting",
            "Professional holiday lighting installation for homes and businesses. Our crews hang, maintain "
            "and remove your lights every season. Request a quote.",
        )],
    )
    assert result.qualification_status == "Qualified"
    assert result.holiday_service == "Yes"


def test_permanent_decorative_lighting_installer_qualifies() -> None:
    result = _classify(
        "glowline-test.com",
        [(
            "https://glowline-test.com/permanent-lighting",
            "Permanent Lighting",
            "We install permanent LED lighting along your eaves with millions of colors, app-controlled for every "
            "holiday and game day. No more ladders. Free consultation.",
        )],
    )
    assert result.qualification_status == "Qualified"
    assert result.permanent_service == "Yes"
    assert "Permanent Lighting Installation" in result.qualified_niches


def test_roofline_holiday_lighting_installer_qualifies() -> None:
    result = _classify(
        "rooflinepros-test.com",
        [(
            "https://rooflinepros-test.com",
            "Roofline Pros",
            "Every November our team installs roofline lighting with commercial-grade C9 Christmas bulbs, then "
            "removes it in January. Get a quote.",
        )],
    )
    assert result.qualification_status == "Qualified"
    assert result.roofline_service == "Yes"
    assert "Roofline Lighting Installation" in result.qualified_niches


def test_electrician_selling_branded_permanent_holiday_lighting_qualifies() -> None:
    result = _classify(
        "ellowitz-test.com",
        [(
            "https://ellowitz-test.com",
            "Ellowitz Electric",
            ELECTRICIAN_HOME
            + "Jellyfish Lighting: dive into stunning illumination with our newest addition, Jellyfish outdoor lighting!",
        )],
    )
    assert result.qualification_status == "Qualified"
    assert result.permanent_service == "Yes"


# --------------------------------------------------------------------------- unrelated businesses rejected


def test_generic_electrician_is_rejected() -> None:
    result = _classify("sparky-test.com", [("https://sparky-test.com", "Sparky Electric", ELECTRICIAN_HOME * 3)])
    assert result.qualification_status == "Rejected"


def test_landscaper_with_only_landscape_lighting_is_rejected() -> None:
    result = _classify(
        "greenscape-test.com",
        [(
            "https://greenscape-test.com/outdoor-lighting",
            "Outdoor Lighting | Greenscape Landscaping",
            "Greenscape Landscaping installs landscape lighting, pathway lighting, garden lighting and uplighting. "
            "We also install permanent outdoor lighting for your patio. Lawn care and irrigation. Free estimate. " * 2,
        )],
    )
    assert result.qualification_status == "Rejected"
    assert result.permanent_service == "No"


def test_plumber_with_no_lighting_services_is_rejected() -> None:
    result = _classify(
        "flowright-test.com",
        [(
            "https://flowright-test.com",
            "FlowRight Plumbing & Heating",
            "Drain cleaning, water heaters, boiler repair and emergency plumbing. Happy holidays from our family! "
            "Licensed and insured. Call for a free estimate. " * 3,
        )],
    )
    assert result.qualification_status == "Rejected"


def test_lighting_retailer_without_installation_is_rejected() -> None:
    result = _classify(
        "lightshop-test.com",
        [(
            "https://lightshop-test.com/collections/christmas-lights",
            "Christmas Lights | Light Shop",
            "C9 Christmas lights, $19.99 per strand. Add to cart. Free shipping on orders over $50. In stock. "
            "Shop now for holiday decor and LED string lights. View cart.",
        )],
    )
    assert result.qualification_status == "Rejected"


def test_decor_retailer_product_page_is_rejected() -> None:
    result = _classify(
        "derian-test.com",
        [(
            "https://derian-test.com/collections/holiday-trees/products/jeweled-tree",
            "Jeweled Crystal Tree | Glass Holiday Decor at Derian Company",
            "The allure of something sparkly cannot be denied, particularly around the holidays. Hand-blown glass "
            "ornament, family owned studio.",
        )],
    )
    assert result.qualification_status != "Qualified"


def test_inaccessible_website_is_unverified_never_qualified() -> None:
    crawl = CrawlResult(
        domain="holidaypros-down-test.com",
        base_url="https://holidaypros-down-test.com",
        is_accessible=False,
        status_category="Timeout",
        error_message="Timed out after 3 attempts",
    )
    result = ChristmasLightClassifier().evaluate_crawl(crawl, company_name="Holiday Light Installation Pros")
    assert result.qualification_status == "Unverified"
    assert result.qualified_niches == []


def test_blocked_site_with_service_metadata_needs_review_not_qualified() -> None:
    crawl = CrawlResult(
        domain="blocked-test.com",
        base_url="https://blocked-test.com",
        is_accessible=False,
        status_category="Bot Challenge",
        error_message="Website protected by web firewall / bot challenge (not bypassed)",
        homepage=CrawledPage(
            url="https://blocked-test.com",
            status_code=403,
            title="Christmas Light Installation | Blocked Co",
            meta_description="Professional Christmas light installation and takedown.",
        ),
    )
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.qualification_status == "Needs Review"
    assert "Metadata Only" in result.website_status
    assert result.christmas_service == "Unclear"


def test_lighting_mentioned_only_in_unrelated_context_is_not_qualified() -> None:
    result = _classify(
        "newtonsupply-test.com",
        [
            ("https://newtonsupply-test.com", "Newton Electrical Supply", "Electrical supply house. Family owned. " * 10),
            (
                "https://newtonsupply-test.com/christmas-lights",
                "Christmas Lights Contest | Newton Electrical Supply",
                "Enter our annual Christmas lights contest! Send us a photo of your display; winners get a gift card.",
            ),
        ],
    )
    assert result.qualification_status != "Qualified"


# --------------------------------------------------------------------------- production false-positive patterns


def test_blog_safety_tips_about_hanging_lights_do_not_qualify() -> None:
    result = _classify(
        "castle-test.com",
        [
            ("https://castle-test.com", "Castle Electric", ELECTRICIAN_HOME * 2),
            (
                "https://castle-test.com/outdoor-holiday-lighting-safety-tips",
                "Outdoor Holiday Lighting Safety Tips",
                "Follow these steps for safe Christmas light installation: Step 1: measure your decorating area. "
                "Professional Christmas light installers recommend outdoor-rated extension cords.",
            ),
        ],
    )
    assert result.qualification_status == "Rejected"


def test_blog_post_tips_for_hanging_lights_do_not_qualify() -> None:
    result = _classify(
        "eliteroof-test.com",
        [
            ("https://eliteroof-test.com", "Elite Roofing", "Roofing, gutter cleaning and repairs. Licensed & insured. " * 5),
            (
                "https://eliteroof-test.com/post/tips-and-tricks-on-how-to-perfectly-hang-holiday-lights",
                "Tips and Tricks on How to Perfectly Hang Holiday Lights",
                "Homeowners start to put up their holiday lights. Some hire a professional to hang their lights but if "
                "you're a DIY person, here is how to hang holiday lights from roof peaks.",
            ),
        ],
    )
    assert result.qualification_status == "Rejected"


def test_soffit_and_roofline_fixtures_without_holiday_context_are_rejected() -> None:
    result = _classify(
        "gath-test.com",
        [(
            "https://gath-test.com/lighting-services",
            "Lighting Services | Gath Electric",
            ELECTRICIAN_HOME
            + "We install pendants, sconces, exterior soffit lighting and roofline lights that make a house look "
            "finished at night.",
        )],
    )
    assert result.qualification_status == "Rejected"
    assert result.roofline_service == "No"


def test_event_company_permanent_venue_lighting_is_rejected() -> None:
    result = _classify(
        "ramar-test.com",
        [(
            "https://ramar-test.com/services/event-lighting",
            "Event Lighting | Ramar Event Productions",
            "Wedding lighting, stage lighting and DJ services. Do you offer permanent lighting installations for "
            "venues? Yes, we design and install permanent lighting for barns hosting events. Request a quote.",
        )],
    )
    assert result.qualification_status == "Rejected"


def test_holiday_decorating_without_lights_needs_review() -> None:
    result = _classify(
        "metrogarden-test.com",
        [(
            "https://metrogarden-test.com/seasonal-holiday-decorating",
            "Holiday Decorating | Metro Garden Design",
            "Landscaping and garden design. We install seasonal decorations, wreaths and garland, and deliver "
            "Christmas trees. Free estimate.",
        )],
    )
    assert result.qualification_status == "Needs Review"
    assert result.qualified_niches == []


def test_holiday_lighting_nav_item_alone_needs_review() -> None:
    """A bare "Holiday Lighting" menu entry on an electrician site is a lead to review, not a qualification."""
    result = _classify(
        "spec-electric-test.com",
        [(
            "https://spec-electric-test.com",
            "Spec Electric",
            ELECTRICIAN_HOME + "| Services | Panel Upgrades | EV Chargers | Holiday Lighting | Generators | Contact |",
        )],
    )
    assert result.qualification_status == "Needs Review"


def test_separate_menu_items_do_not_combine_into_a_service_statement(tmp_path: Path) -> None:
    crawler = WebsiteCrawler(cache_dir=tmp_path, config=CrawlerConfig(block_separators=True))
    html = (
        "<html><head><title>Lighthouse Outdoor Lighting</title></head><body><nav><ul>"
        "<li><a href='/design'>Design</a></li><li><a href='/install'>Lighting Installation</a></li>"
        "<li><a href='/care'>Service &amp; Legacy Care</a></li><li><a href='/events'>Holiday &amp; Event Lighting</a></li>"
        "</ul></nav><main><p>Landscape lighting and architectural lighting for fine homes.</p></main></body></html>"
    )
    _, _, text, _ = crawler._clean_text_from_html(html)
    assert "Lighting Installation | Service & Legacy Care | Holiday & Event Lighting" in text
    result = _classify("lighthouse-test.com", [("https://lighthouse-test.com", "Lighthouse Outdoor Lighting", text)])
    assert result.qualification_status != "Qualified"


def test_default_crawler_text_extraction_is_unchanged(tmp_path: Path) -> None:
    crawler = WebsiteCrawler(cache_dir=tmp_path, config=CrawlerConfig())
    _, _, text, _ = crawler._clean_text_from_html("<html><body><ul><li>One</li><li>Two</li></ul></body></html>")
    assert text == "One Two"


def test_christmas_crawler_config_uses_block_separators() -> None:
    assert christmas_crawler_config().block_separators is True


# --------------------------------------------------------------------------- enricher outputs and checkpoint


class _StaticCrawler(WebsiteCrawler):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self.calls = calls

    async def crawl_domain(self, base_url: str) -> CrawlResult:
        self.calls.append(base_url)
        return _crawl(
            "roofline-test.com",
            [(
                "https://roofline-test.com/permanent-holiday-lighting",
                "Permanent Holiday Lighting",
                "We install permanent holiday lighting along your roofline. Free estimate. Our installers are insured.",
            )],
        )


def _write_leads(path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "website", "city", "state"])
        w.writerow(["Roofline Co", "https://roofline-test.com/", "Austin", "TX"])


def test_outputs_record_niches_and_stale_checkpoint_is_not_reused(tmp_path: Path) -> None:
    in_csv = tmp_path / "leads.csv"
    _write_leads(in_csv)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    fingerprint = ChristmasLightLeadEnricher._file_fingerprint(in_csv)
    key = ChristmasLightLeadEnricher._crawl_key("https://roofline-test.com/")
    stale = {
        "fingerprint": fingerprint,
        "classifier_version": "old-rules",
        "key": key,
        "url": "https://roofline-test.com/",
        "result": {"classification": "No", "decision_label": "No", "confidence": 0.9, "website_status": "OK"},
    }
    (out_dir / "crawl_checkpoint.jsonl").write_text(json.dumps(stale) + "\n", encoding="utf-8")

    calls: list[str] = []
    enricher = ChristmasLightLeadEnricher(crawler=_StaticCrawler(calls), concurrency_limit=1)
    telemetry = asyncio.run(enricher.process_leads(in_csv, out_dir, resume=True))

    assert calls == ["https://roofline-test.com/"]  # stale verdict ignored, site re-evaluated
    assert telemetry.qualified_count == 1
    with open(out_dir / "qualified_leads.csv", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    for col in ENRICHMENT_HEADERS:
        assert col in header
    niches = rows[1][header.index("Qualified_Niches")]
    assert "Permanent Lighting Installation" in niches
    assert "Roofline Lighting Installation" in niches
    assert rows[1][header.index("Roofline_Lighting_Installation")] == "Yes"

    last = json.loads((out_dir / "crawl_checkpoint.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert last["classifier_version"] == CLASSIFIER_VERSION
