"""Tests for the thorough Christmas / holiday / permanent lighting crawl, classification, and enrichment engine."""

import asyncio
import csv
import json
from pathlib import Path

import httpx
import pytest

from contractor_scraper.classification.christmas_light_classifier import ChristmasLightClassifier
from contractor_scraper.discovery.crawler import CrawledPage, CrawlerConfig, CrawlResult, WebsiteCrawler
from contractor_scraper.orchestration.christmas_light_enricher import (
    ENRICHMENT_HEADERS,
    ChristmasLightLeadEnricher,
)

FAST_CONFIG = CrawlerConfig(
    connect_timeout=2.0,
    read_timeout=2.0,
    domain_timeout=20.0,
    max_retries=3,
    backoff_base=0.01,
    backoff_max=0.05,
    per_domain_concurrency=2,
    respect_robots_txt=True,
    use_sitemap=True,
    impersonate_search_bot_on_challenge=False,
    cache_namespace="test_crawler",
)


def _html(title: str, body: str, links: str = "") -> str:
    return f"<html><head><title>{title}</title></head><body><nav>{links}</nav><main>{body}</main></body></html>"


def _install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


LANDSCAPER_HOME = _html(
    "Evergreen Landscaping | Lawn Care & Outdoor Services",
    "Evergreen Landscaping provides lawn care, landscape design, mulching, and seasonal cleanups for homes "
    "across the county. Family owned and operated. Request a free estimate today. " * 3,
)


# --------------------------------------------------------------------------- crawler


def test_crawler_retries_transient_503_with_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"home": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path in ("", "/"):
            calls["home"] += 1
            if calls["home"] < 3:
                return httpx.Response(503, headers={"Retry-After": "0"})
            return httpx.Response(200, html=LANDSCAPER_HOME)
        return httpx.Response(404)

    _install_mock_transport(monkeypatch, handler)
    crawler = WebsiteCrawler(cache_dir=tmp_path, config=FAST_CONFIG)
    result = asyncio.run(crawler.crawl_domain("https://evergreen-test.com"))

    assert result.is_accessible is True
    assert calls["home"] == 3
    assert result.total_attempts >= 3


def test_crawler_reports_dns_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

    _install_mock_transport(monkeypatch, handler)
    crawler = WebsiteCrawler(cache_dir=tmp_path, config=FAST_CONFIG)
    result = asyncio.run(crawler.crawl_domain("https://no-such-host-test.com"))

    assert result.is_accessible is False
    assert result.status_category == "DNS Failure"


def test_crawler_respects_robots_and_discovers_sitemap_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []
    holiday_page = _html(
        "Permanent Christmas Lights | Evergreen",
        "We install permanent Christmas lights along your roofline. Our installers handle design and installation. " * 3,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fetched.append(path)
        if path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /private-holiday\nSitemap: https://evergreen-test.com/sitemap.xml\n",
            )
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text="<urlset><url><loc>https://evergreen-test.com/winter/permanent-christmas-lights</loc></url></urlset>",
                headers={"content-type": "application/xml"},
            )
        if path in ("", "/"):
            return httpx.Response(200, html=LANDSCAPER_HOME.replace("<nav></nav>", '<nav><a href="/private-holiday">Holiday Lighting</a></nav>'))
        if path == "/winter/permanent-christmas-lights":
            return httpx.Response(200, html=holiday_page)
        return httpx.Response(404)

    _install_mock_transport(monkeypatch, handler)
    crawler = WebsiteCrawler(cache_dir=tmp_path, config=FAST_CONFIG)
    result = asyncio.run(crawler.crawl_domain("https://evergreen-test.com"))

    assert "/private-holiday" not in fetched
    assert any("private-holiday" in u for u in result.robots_blocked_urls)
    assert result.sitemap_checked is True
    assert any("permanent-christmas-lights" in p.url for p in result.subpages)

    verdict = ChristmasLightClassifier().evaluate_crawl(result, company_name="Evergreen Landscaping")
    assert verdict.classification == "Yes"
    assert verdict.permanent_service == "Yes"
    assert any("permanent-christmas-lights" in u for u in verdict.evidence_urls)


def test_crawler_discards_soft_404_probe_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        # Every path returns the homepage (common CMS soft-404 behaviour)
        return httpx.Response(200, html=LANDSCAPER_HOME)

    _install_mock_transport(monkeypatch, handler)
    crawler = WebsiteCrawler(cache_dir=tmp_path, config=FAST_CONFIG)
    result = asyncio.run(crawler.crawl_domain("https://evergreen-test.com"))

    assert result.is_accessible is True
    assert result.subpages == []
    assert result.probes_checked is True


# --------------------------------------------------------------------------- classifier


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


def test_landscaper_with_dedicated_holiday_lighting_page_qualifies() -> None:
    crawl = _crawl(
        "evergreen.com",
        [
            ("https://evergreen.com", "Evergreen Landscaping", "Lawn care, landscape design, mulching. Family owned and operated. Free estimate."),
            ("https://evergreen.com/holiday-lighting", "Holiday Lighting | Evergreen", "Holiday Lighting. Make your home shine this season. Call for a free estimate."),
        ],
    )
    result = ChristmasLightClassifier().evaluate_crawl(crawl, company_name="Evergreen Landscaping")
    assert result.classification == "Yes"
    assert result.holiday_service == "Yes"
    assert "https://evergreen.com/holiday-lighting" in result.evidence_urls


def test_retailer_selling_christmas_lights_not_qualified() -> None:
    crawl = _crawl(
        "lightsupplyshop.com",
        [
            (
                "https://lightsupplyshop.com",
                "Christmas Lights Shop",
                "Shop now for Christmas lights and holiday decor. Add to cart. $19.99 per strand. "
                "Free shipping on orders over $50. View cart. Proceed to checkout. In stock.",
            )
        ],
    )
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "No"
    assert "product seller" in result.reasoning.lower() or "e-commerce" in result.reasoning.lower()


def test_permanent_landscape_lighting_is_not_permanent_decorative_lighting() -> None:
    text = (
        "We design and install permanent landscape lighting: pathway lighting, garden lighting, tree uplighting, "
        "and patio lighting for homes. Licensed and insured. Request a quote. " * 3
    )
    crawl = _crawl("brightpathlandscape.com", [("https://brightpathlandscape.com", "Bright Path Landscape Lighting", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "No"
    assert result.permanent_service == "No"
    assert "landscape" in result.reasoning.lower()


def test_permanent_lighting_mention_without_installation_is_uncertain() -> None:
    text = (
        "Smith Electric handles panel upgrades and rewiring. Read our blog: what homeowners should know about "
        "permanent lighting trends this year. " * 3
    )
    crawl = _crawl("smithelectric.com", [("https://smithelectric.com/blog/trends", "Blog | Smith Electric", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "Uncertain"
    assert result.permanent_service == "Unclear"


def test_trimlight_dealer_qualifies_as_permanent_decorative_lighting() -> None:
    text = (
        "Authorized Trimlight dealer. We install permanent LED roofline lighting with millions of colors, "
        "controlled from your phone app, for every holiday and game day. Free consultation."
    )
    crawl = _crawl("glowlinepros.com", [("https://glowlinepros.com/permanent-lighting", "Permanent Lighting", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "Yes"
    assert result.permanent_service == "Yes"


def test_negated_service_statement_does_not_qualify() -> None:
    text = (
        "Oak Ridge Lawn Care offers mowing, aeration, and leaf removal. Please note: we no longer offer "
        "Christmas light installation. Family owned. Free estimate. " * 2
    )
    crawl = _crawl("oakridgelawn.com", [("https://oakridgelawn.com", "Oak Ridge Lawn Care", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification != "Yes"


def test_holiday_light_attraction_not_qualified() -> None:
    text = (
        "Festival of Lights at Riverside Park! Drive-thru light show with over a million holiday lights. "
        "Buy tickets online; admission is $25 per car. Open nightly through January. " * 2
    )
    crawl = _crawl("riversidelights.org", [("https://riversidelights.org", "Festival of Lights", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "No"


def test_unreachable_site_not_qualified_by_business_name_alone() -> None:
    crawl = CrawlResult(
        domain="christmaslightspros-test.com",
        base_url="https://christmaslightspros-test.com",
        is_accessible=False,
        status_category="Timeout",
        error_message="Timed out after 3 attempts",
    )
    result = ChristmasLightClassifier().evaluate_crawl(crawl, company_name="Christmas Light Installation Pros")
    assert result.classification == "Unreachable"
    assert result.christmas_service == "Unclear"
    assert "manual verification" in result.reasoning.lower()


# --------------------------------------------------------------------------- enricher


class _SpyCrawler(WebsiteCrawler):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self.calls = calls

    async def crawl_domain(self, base_url: str) -> CrawlResult:
        self.calls.append(base_url)
        return _crawl(
            "holidaypros-test.com",
            [
                (
                    "https://holidaypros-test.com/christmas-light-installation",
                    "Christmas Light Installation",
                    "Professional Christmas light installation, takedown and storage. Free estimate. Our installers are fully insured.",
                )
            ],
        )


def _write_input(path: Path) -> None:
    headers = ["name", "website", "Clean Website Url's", "city", "state", "email", "email"]
    rows = [
        ["Holiday Pros", "https://holidaypros-test.com/", "https://holidaypros-test.com/", "Denver", "CO", "a@x.com", "a@x.com"],
        ["Holiday Pros", "https://holidaypros-test.com/", "https://holidaypros-test.com/", "Denver", "CO", "a@x.com", "a@x.com"],
        ["Holiday Pros", "https://holidaypros-test.com/", "https://holidaypros-test.com/", "Denver", "CO", "b@x.com", "b@x.com"],
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def test_enricher_dedupes_rows_crawls_domain_once_and_validates(tmp_path: Path) -> None:
    in_csv = tmp_path / "leads.csv"
    _write_input(in_csv)
    calls: list[str] = []
    enricher = ChristmasLightLeadEnricher(crawler=_SpyCrawler(calls), concurrency_limit=2)
    out_dir = tmp_path / "out"
    telemetry = asyncio.run(enricher.process_leads(input_csv_path=in_csv, output_dir=out_dir, resume=False))

    assert len(calls) == 1
    assert telemetry.duplicate_rows_removed == 1
    assert telemetry.qualified_count == 2
    assert telemetry.validation["passed"] is True

    with open(out_dir / "qualified_leads.csv", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    for col in ENRICHMENT_HEADERS:
        assert col in header
    assert header.count("email") == 2  # duplicate original columns preserved
    assert len(rows) == 3
    assert rows[1][header.index("Qualification_Status")] == "Qualified"
    assert rows[1][header.index("Christmas_Light_Installation")] == "Yes"
    assert "christmas-light-installation" in rows[1][header.index("Evidence_URLs")]

    report = json.loads((out_dir / "run_report.json").read_text(encoding="utf-8"))
    assert report["totals"]["qualified"] == 2


def test_enricher_resumes_from_checkpoint_without_recrawling(tmp_path: Path) -> None:
    in_csv = tmp_path / "leads.csv"
    _write_input(in_csv)
    out_dir = tmp_path / "out"
    first_calls: list[str] = []
    asyncio.run(
        ChristmasLightLeadEnricher(crawler=_SpyCrawler(first_calls)).process_leads(in_csv, out_dir, resume=False)
    )
    second_calls: list[str] = []
    telemetry = asyncio.run(
        ChristmasLightLeadEnricher(crawler=_SpyCrawler(second_calls)).process_leads(in_csv, out_dir, resume=True)
    )
    assert len(first_calls) == 1
    assert second_calls == []
    assert telemetry.websites_from_checkpoint == 1
    assert telemetry.qualified_count == 2


def test_ornament_shop_hanging_ornaments_not_qualified() -> None:
    text = (
        "Serene Spaces Living set of 12 glass balls hanging ornaments for holiday decor. $24.99. Add to cart. "
        "Sold out. Free shipping on orders over $50. Your cart is empty. Continue shopping. " * 2
    )
    crawl = _crawl("ornamentshop-test.com", [("https://ornamentshop-test.com/products/ornaments", "Hanging Ornaments", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "No"


def test_holiday_light_safety_tip_is_not_a_service_offer() -> None:
    text = (
        "128 Plumbing, Heating and Cooling. Furnace repair, boiler installation, and AC service. Licensed and insured. "
        "Safety Tips: Before hanging holiday lights, check for damaged sockets, plugs and cords. Free estimate. " * 2
    )
    crawl = _crawl("plumbing-test.com", [("https://plumbing-test.com/blog/safety", "Winter Safety Tips", text)])
    result = ChristmasLightClassifier().evaluate_crawl(crawl)
    assert result.classification == "No"
    assert result.holiday_service == "No"


def _archived_crawl(ts: str) -> CrawlResult:
    crawl = _crawl(
        "blocked-installer-test.com",
        [
            (
                f"https://web.archive.org/web/{ts}id_/https://blocked-installer-test.com/christmas-lights",
                "Christmas Light Installation",
                "Professional Christmas light installation and takedown. Free estimate. Fully insured crew.",
            )
        ],
    )
    crawl.archived_snapshot = ts
    return crawl


def test_archived_snapshot_verdict_is_discounted_and_stale_snapshots_are_uncertain() -> None:
    enricher = ChristmasLightLeadEnricher(crawler=_SpyCrawler([]))
    live = CrawlResult(
        domain="blocked-installer-test.com",
        base_url="https://blocked-installer-test.com",
        is_accessible=False,
        status_category="Bot Challenge",
        error_message="Website protected by web firewall / bot challenge (not bypassed)",
    )
    live_result = ChristmasLightClassifier().evaluate_crawl(live)

    recent = enricher._classify_archived(_archived_crawl("20250915000000"), live, live_result, "Blocked Installer")
    assert recent.classification == "Yes"
    assert recent.website_status == "Archived Copy (live: Bot Challenge)"
    assert recent.confidence < 0.95
    assert "web.archive.org" in recent.evidence_urls[0]

    stale = enricher._classify_archived(_archived_crawl("20150101000000"), live, live_result, "Blocked Installer")
    assert stale.classification == "Uncertain"
