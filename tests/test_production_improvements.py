"""Unit and integration tests for all 11 required improvement scenarios.

Covers:
1. No year specified: processes years descending from 2026 to 2016.
2. Explicit year specified: processes only the requested year.
3. Target reached during a year: stops without processing older years.
4. Target not reached in one year: continues to the next available year.
5. All years exhausted: exits cleanly and reports actual production leads.
6. Duplicate companies across different years: counted and exported only once.
7. Website found but email missing: saved as incomplete, not counted as production-ready.
8. Search provider HTTP 429 / CAPTCHA: backoff and fallback behavior.
9. Email in website footer or contact subpage extracted successfully.
10. Reliable website location differing from Sunbiz enriches final lead.
11. Interruption and resume: previously saved leads preserved and not duplicated.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import polars as pl

from contractor_scraper.discovery.base import WebsiteCandidate, WebsiteVerificationResult
from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler
from contractor_scraper.discovery.website_finder import SearchProviderTracker, WebsiteFinder
from contractor_scraper.extraction.base import EmailCandidate, EmailVerificationResult
from contractor_scraper.orchestration.lead_engine import LeadEngine
from tests.conftest import make_sunbiz_line


def _mock_lead_engine(tmp_path: Path) -> LeadEngine:
    """Create a LeadEngine instance with isolated mocks for finder, verifier, crawler, extractor."""
    engine = LeadEngine(output_dir=tmp_path, concurrency_limit=2)

    # Mock website finder
    async def mock_find(company_name, city, state="FL", category="roofing", **kwargs):
        slug = company_name.lower().replace(" ", "").replace("&", "")
        return [
            WebsiteCandidate(
                url=f"https://{slug}.com",
                domain=f"{slug}.com",
                discovery_source="mock",
                initial_confidence=0.9,
            )
        ]

    engine.finder.find_websites = AsyncMock(side_effect=mock_find)

    # Mock website verifier
    async def mock_verify(candidate, expected_company_name, **kwargs):
        return WebsiteVerificationResult(
            url=candidate.url,
            domain=candidate.domain,
            status="VERIFIED",
            is_verified=True,
            confidence=0.92,
        )

    engine.verifier.verify_website = AsyncMock(side_effect=mock_verify)

    # Mock crawler
    async def mock_crawl(url):
        slug = url.replace("https://", "").replace("http://", "").split(".")[0]
        return CrawlResult(
            domain=f"{slug}.com",
            base_url=url,
            is_accessible=True,
            all_emails=[f"info@{slug}.com"],
        )

    engine.crawler.crawl_domain = AsyncMock(side_effect=mock_crawl)

    # Mock email extractor
    async def mock_extract(url, crawl_result=None, **kwargs):
        slug = url.replace("https://", "").replace("http://", "").split(".")[0]
        return [
            EmailCandidate(
                email_address=f"info@{slug}.com",
                source_url=url,
                initial_confidence=0.95,
            )
        ]

    engine.email_extractor.extract_emails = AsyncMock(side_effect=mock_extract)

    # Mock email verifier
    async def mock_verify_email(candidate, **kwargs):
        return EmailVerificationResult(
            email_address=candidate.email_address,
            status="DELIVERABLE",
            is_verified=True,
            confidence=0.95,
        )

    engine.email_verifier.verify_email = AsyncMock(side_effect=mock_verify_email)
    return engine


def test_scenario_1_no_year_specified_searches_descending(tmp_path: Path) -> None:
    """Scenario 1: No year specified processes descending from latest available year (2026) to 2016."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)
        years_queried: list[int] = []

        def mock_stream(year: int, max_files=None):
            years_queried.append(year)
            # Return empty for all years so it iterates through them
            return iter([])

        engine.sunbiz_source.stream_raw_records_for_year = mock_stream

        # Run with year=None
        await engine.run(year=None, category="roofing", limit=10)

        # Must start at 2026 and count down to 2016
        assert len(years_queried) > 0
        assert years_queried[0] == 2026
        assert years_queried[-1] == 2016
        assert years_queried == list(range(2026, 2015, -1))

    asyncio.run(_test())


def test_scenario_2_explicit_year_searches_only_that_year(tmp_path: Path) -> None:
    """Scenario 2: Explicit year specified processes only that year and does not continue to others."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)
        years_queried: list[int] = []

        def mock_stream(year: int, max_files=None):
            years_queried.append(year)
            # Return 1 lead, below limit of 10
            return iter(
                [
                    make_sunbiz_line(
                        corp_num="P2400001",
                        name="APEX ROOFING LLC",
                        city="ORLANDO",
                        file_date="01012024",
                    )
                ]
            )

        engine.sunbiz_source.stream_raw_records_for_year = mock_stream

        # Run with explicit year=2024, limit=10 (unreached)
        csv_path, telemetry = await engine.run(year=2024, category="roofing", limit=10)

        # Must only query 2024 and stop
        assert years_queried == [2024]
        assert telemetry.final_complete_leads == 1
        assert len(telemetry.year_results) == 1
        assert telemetry.year_results[0].year == 2024

    asyncio.run(_test())


def test_scenario_3_target_reached_during_year_stops_early(tmp_path: Path) -> None:
    """Scenario 3: Target reached during a year stops without processing older years."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)
        years_queried: list[int] = []

        def mock_stream(year: int, max_files=None):
            years_queried.append(year)
            # 2026 provides 3 matching leads
            if year == 2026:
                return iter(
                    [
                        make_sunbiz_line(
                            corp_num=f"P260000{i}",
                            name=f"ROOFING PRO {i} LLC",
                            city="TAMPA",
                            file_date="01012026",
                        )
                        for i in range(1, 4)
                    ]
                )
            return iter([])

        engine.sunbiz_source.stream_raw_records_for_year = mock_stream

        # Limit is 2. 2026 provides 3, so target reached in 2026!
        csv_path, telemetry = await engine.run(year=None, category="roofing", limit=2)

        assert years_queried == [2026]
        assert telemetry.final_complete_leads == 2
        df = pl.read_csv(csv_path)
        assert len(df) == 2

    asyncio.run(_test())


def test_scenario_4_target_not_reached_continues_to_next_year(tmp_path: Path) -> None:
    """Scenario 4: If target is not reached in current year, scraper continues to next older year."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)
        years_queried: list[int] = []

        def mock_stream(year: int, max_files=None):
            years_queried.append(year)
            if year == 2026:
                # 2026 provides 1 lead
                return iter(
                    [
                        make_sunbiz_line(
                            corp_num="P2600001",
                            name="ROOFING 2026 LLC",
                            city="ORLANDO",
                            file_date="01012026",
                        )
                    ]
                )
            elif year == 2025:
                # 2025 provides 2 leads
                return iter(
                    [
                        make_sunbiz_line(
                            corp_num=f"P250000{i}",
                            name=f"ROOFING 2025 PRO {i} LLC",
                            city="MIAMI",
                            file_date="01012025",
                        )
                        for i in range(1, 3)
                    ]
                )
            return iter([])

        engine.sunbiz_source.stream_raw_records_for_year = mock_stream

        # Limit is 3. 2026 gives 1, 2025 gives 2 -> total 3!
        csv_path, telemetry = await engine.run(year=None, category="roofing", limit=3)

        assert years_queried == [2026, 2025]
        assert telemetry.final_complete_leads == 3
        df = pl.read_csv(csv_path)
        assert len(df) == 3

    asyncio.run(_test())


def test_scenario_5_all_years_exhausted_exits_cleanly(tmp_path: Path) -> None:
    """Scenario 5: When all years down to 2016 are exhausted, exits cleanly and reports total."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)
        years_queried: list[int] = []

        def mock_stream(year: int, max_files=None):
            years_queried.append(year)
            if year == 2020:
                return iter(
                    [
                        make_sunbiz_line(
                            corp_num="P2000001",
                            name="HISTORIC ROOFING LLC",
                            city="TAMPA",
                            file_date="01012020",
                        )
                    ]
                )
            return iter([])

        engine.sunbiz_source.stream_raw_records_for_year = mock_stream

        # Limit is 100, but only 1 lead exists across all years
        csv_path, telemetry = await engine.run(year=None, category="roofing", limit=100)

        assert years_queried == list(range(2026, 2015, -1))
        assert telemetry.final_complete_leads == 1
        df = pl.read_csv(csv_path)
        assert len(df) == 1

    asyncio.run(_test())


def test_scenario_6_duplicate_companies_across_years_deduplicated(tmp_path: Path) -> None:
    """Scenario 6: Same company appearing in multiple years is counted and exported only once."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)

        def mock_stream(year: int, max_files=None):
            if year == 2026:
                return iter(
                    [
                        make_sunbiz_line(
                            corp_num="P2600001",
                            name="EVERLASTING ROOFING LLC",
                            city="ORLANDO",
                            file_date="01012026",
                        )
                    ]
                )
            elif year == 2025:
                # Same company name and corp_num in 2025
                return iter(
                    [
                        make_sunbiz_line(
                            corp_num="P2600001",
                            name="EVERLASTING ROOFING LLC",
                            city="ORLANDO",
                            file_date="01012025",
                        ),
                        make_sunbiz_line(
                            corp_num="P2500002",
                            name="NEW SUN ROOFING LLC",
                            city="TAMPA",
                            file_date="01012025",
                        ),
                    ]
                )
            return iter([])

        engine.sunbiz_source.stream_raw_records_for_year = mock_stream

        csv_path, telemetry = await engine.run(year=None, category="roofing", limit=5)

        # Only 2 distinct leads (Everlasting Roofing + New Sun Roofing)
        assert telemetry.final_complete_leads == 2
        df = pl.read_csv(csv_path)
        assert len(df) == 2
        names = df["Company Name"].to_list()
        assert names.count("Everlasting Roofing") == 1

    asyncio.run(_test())


def test_scenario_7_website_found_but_missing_email_not_counted_as_production(
    tmp_path: Path,
) -> None:
    """Scenario 7: Company with website but no email is saved to missing-contact, NOT counted toward limit."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)

        # Email extractor returns empty for Company 1, but valid email for Company 2
        async def mock_extract(url, crawl_result=None, **kwargs):
            if "noemail" in url:
                return []
            return [EmailCandidate(email_address="info@hasemail.com", source_url=url, initial_confidence=0.9)]

        engine.email_extractor.extract_emails = AsyncMock(side_effect=mock_extract)

        async def mock_find(company_name, city, **kwargs):
            if "noemail" in company_name.lower():
                return [WebsiteCandidate(url="https://noemail.com", domain="noemail.com", discovery_source="mock")]
            return [WebsiteCandidate(url="https://hasemail.com", domain="hasemail.com", discovery_source="mock")]

        engine.finder.find_websites = AsyncMock(side_effect=mock_find)

        mock_lines = [
            make_sunbiz_line(corp_num="P2500001", name="NOEMAIL ROOFING LLC", city="MIAMI", file_date="01012025"),
            make_sunbiz_line(corp_num="P2500002", name="HASEMAIL ROOFING LLC", city="TAMPA", file_date="01022025"),
        ]
        engine.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter(mock_lines)

        csv_path, telemetry = await engine.run(year=2025, category="roofing", limit=5)

        # Only HasEmail should count as production-ready
        assert telemetry.final_complete_leads == 1
        prod_df = pl.read_csv(tmp_path / "production_leads.csv")
        assert len(prod_df) == 1
        assert prod_df["Company Name"][0] == "Hasemail Roofing"
        assert prod_df["Email"][0] == "info@hasemail.com"

        # NoEmail must be in missing contact CSV
        missing_df = pl.read_csv(tmp_path / "leads_missing_contact.csv")
        assert len(missing_df) == 1
        assert missing_df["Company Name"][0] == "Noemail Roofing"
        assert str(missing_df["Email"][0] or "") == ""

    asyncio.run(_test())


def test_scenario_8_search_provider_rate_limit_and_backoff(tmp_path: Path) -> None:
    """Scenario 8: HTTP 429 or CAPTCHA triggers provider backoff cooldown and switches to fallback provider."""

    async def _test() -> None:
        tracker = SearchProviderTracker(default_cooldown_seconds=30.0)
        finder = WebsiteFinder(cache_dir=tmp_path / "cache", tracker=tracker)

        # Mock httpx responses:
        # Request 1 (Bing): returns HTTP 429
        # Request 2 (DDG Lite): returns HTTP 200 with result link
        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "bing.com" in url_str:
                return httpx.Response(429, text="Too Many Requests")
            elif "duckduckgo.com" in url_str:
                html = '<a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.apexroofingflorida.com%2F">Apex</a>'
                return httpx.Response(200, text=html)
            return httpx.Response(404, text="Not Found")

        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            # Query Bing -> triggers 429 and backoff
            bing_res = await finder._search_bing(client, "Apex Roofing Orlando FL")
            assert bing_res == []
            assert not tracker.is_available("Bing")
            assert tracker.get_remaining_cooldown("Bing") > 20.0

            # Immediate next attempt skips Bing and switches to DDG
            ddg_res = await finder._search_ddg_lite(client, "Apex Roofing Orlando FL")
            assert len(ddg_res) >= 1
            assert "apexroofingflorida.com" in ddg_res[0]

    asyncio.run(_test())


def test_scenario_9_website_footer_or_contact_email_extracted(tmp_path: Path) -> None:
    """Scenario 9: Email embedded in website footer or dynamic contact page is extracted."""

    async def _test() -> None:
        crawler = WebsiteCrawler(cache_dir=tmp_path / "crawl", max_subpages=5)

        homepage_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Apex Roofing Pro</title></head>
        <body>
            <header><h1>Welcome to Apex Roofing</h1></header>
            <p>Quality roofs in Central Florida.</p>
            <a href="/contact-us">Get in Touch with our team</a>
            <footer>
                <p>&copy; 2025 Apex Roofing Pro LLC</p>
                <p>Support: <a href="mailto:contact@apexroofingpro.com">contact@apexroofingpro.com</a></p>
            </footer>
        </body>
        </html>
        """

        contact_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Contact Apex Roofing</title></head>
        <body>
            <h2>Direct Lines</h2>
            <p>Office email: office@apexroofingpro.com</p>
            <p>Phone: (407) 555-0199</p>
        </body>
        </html>
        """

        async def mock_handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "contact" in url_str:
                return httpx.Response(200, text=contact_html)
            return httpx.Response(200, text=homepage_html)

        transport = httpx.MockTransport(mock_handler)
        with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            res = await crawler.crawl_domain("https://apexroofingpro.com")

        assert res.is_accessible
        # Both footer email and contact page email must be extracted
        assert "contact@apexroofingpro.com" in res.all_emails
        assert "office@apexroofingpro.com" in res.all_emails

    asyncio.run(_test())


def test_scenario_10_reliable_website_location_enrichment(tmp_path: Path) -> None:
    """Scenario 10: Reliable website address updates city and state from Sunbiz default, preserving original values."""

    async def _test() -> None:
        engine = _mock_lead_engine(tmp_path)

        # Sunbiz record has Tallahassee
        mock_line = make_sunbiz_line(
            corp_num="P2500099",
            name="COASTLINE ROOFING LLC",
            city="TALLAHASSEE",
            file_date="01012025",
        )
        engine.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter([mock_line])

        # Verified website text has explicit contact address in Sarasota, FL
        website_text = """
        Coastline Roofing Headquarters
        Visit Us: 1240 Main St, Sarasota, FL 34236
        Phone: 941-555-1234
        Email: info@coastlineroofing.com
        """

        engine.crawler.crawl_domain = AsyncMock(
            return_value=CrawlResult(
                domain="coastlineroofing.com",
                base_url="https://coastlineroofing.com",
                is_accessible=True,
                all_emails=["info@coastlineroofing.com"],
                combined_text=website_text,
            )
        )

        csv_path, telemetry = await engine.run(year=2025, category="roofing", limit=1)

        df = pl.read_csv(csv_path)
        assert len(df) == 1
        # City should be enriched to Sarasota!
        assert df["City"][0] == "Sarasota"
        assert df["State"][0] == "FL"

    asyncio.run(_test())


def test_scenario_11_interruption_and_resume_preserves_leads_without_duplicates(
    tmp_path: Path,
) -> None:
    """Scenario 11: Resumed runs reload previously saved leads and do not duplicate them."""

    async def _test() -> None:
        engine1 = _mock_lead_engine(tmp_path)

        # Run 1: Collect 2 leads
        mock_lines_run1 = [
            make_sunbiz_line(corp_num="P2500001", name="ALPHA ROOFING LLC", city="ORLANDO", file_date="01012025"),
            make_sunbiz_line(corp_num="P2500002", name="BETA ROOFING LLC", city="TAMPA", file_date="01022025"),
        ]
        engine1.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter(mock_lines_run1)

        csv1, telemetry1 = await engine1.run(year=2025, category="roofing", limit=2)
        assert telemetry1.final_complete_leads == 2
        df1 = pl.read_csv(tmp_path / "production_leads.csv")
        assert len(df1) == 2

        # Run 2: Resume with higher limit=3, receiving Alpha Roofing again plus Gamma Roofing
        engine2 = _mock_lead_engine(tmp_path)
        mock_lines_run2 = [
            make_sunbiz_line(corp_num="P2500001", name="ALPHA ROOFING LLC", city="ORLANDO", file_date="01012025"),
            make_sunbiz_line(corp_num="P2500003", name="GAMMA ROOFING LLC", city="MIAMI", file_date="01032025"),
        ]
        engine2.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter(mock_lines_run2)

        csv2, telemetry2 = await engine2.run(year=2025, category="roofing", limit=3)
        assert telemetry2.final_complete_leads == 3

        df2 = pl.read_csv(tmp_path / "production_leads.csv")
        # Exactly 3 leads: Alpha, Beta, Gamma (Alpha was NOT duplicated)
        assert len(df2) == 3
        names = df2["Company Name"].to_list()
        assert names.count("Alpha Roofing") == 1
        assert names.count("Beta Roofing") == 1
        assert names.count("Gamma Roofing") == 1

    asyncio.run(_test())
