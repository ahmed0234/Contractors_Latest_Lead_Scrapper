"""Comprehensive tests for Christmas light installation qualification and enrichment improvements."""

import csv
from pathlib import Path
import pytest

from contractor_scraper.classification.christmas_light_classifier import (
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler
from contractor_scraper.orchestration.christmas_light_enricher import ChristmasLightLeadEnricher


class ComprehensiveMockCrawler(WebsiteCrawler):
    """Mock crawler simulating various real-world contractor websites."""

    async def crawl_domain(self, base_url: str) -> CrawlResult:
        u = base_url.lower()

        # 1. Pure Christmas lighting company
        if "pure-christmas" in u:
            return CrawlResult(
                domain="pure-christmas.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Welcome to Premier Christmas Light Installation. We specialize in residential "
                    "and commercial holiday lighting installation, takedown, and storage. "
                    "Get a free estimate today for your custom holiday illumination."
                ),
            )

        # 2. Multi-trade landscaper that offers seasonal Christmas lighting
        elif "green-acres-landscaping" in u:
            return CrawlResult(
                domain="green-acres-landscaping.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Green Acres Landscaping offers lawn care, sod installation, and hardscaping. "
                    "During the winter season, we provide full-service Christmas light installation, "
                    "custom holiday lighting design, hanging, and removal for homeowners across the metro area. "
                    "Contact our team for a free estimate!"
                ),
            )

        # 3. Window cleaner that offers holiday light hanging
        elif "crystal-clear-windows" in u:
            return CrawlResult(
                domain="crystal-clear-windows.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Crystal Clear Window Washing and Gutter Cleaning. When winter arrives, we hang "
                    "holiday lights! Professional Christmas lighting installation and takedown services. "
                    "Fully insured crew. Request a quote today."
                ),
            )

        # 4. Landscaper that only offers landscape lighting (NO holiday lighting)
        elif "pure-landscaping-only" in u:
            return CrawlResult(
                domain="pure-landscaping-only.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Quality Lawn and Landscape Co. We provide lawn mowing, aeration, mulching, "
                    "and low voltage landscape lighting installation. Architectural garden uplighting "
                    "and pathway illumination. Call for a consultation."
                ),
            )

        # 5. Landscaper with casual holiday greeting (NO holiday lighting)
        elif "holiday-hours-landscaper" in u:
            return CrawlResult(
                domain="holiday-hours-landscaper.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Oak Valley Landscaping. We provide lawn maintenance, patio design, and sod installation. "
                    "Please note our holiday hours: closed for Christmas and New Year. "
                    "Happy holidays from our family to yours!"
                ),
            )

        # 6. Event and DJ lighting company (strictly excluded)
        elif "sound-and-stage-lighting" in u:
            return CrawlResult(
                domain="sound-and-stage-lighting.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Elite DJ & Event Productions. We provide concert sound, stage lighting, "
                    "wedding uplighting, dance floor illumination, and party lighting for corporate galas. "
                    "Book our DJ and lighting packages for your next big event."
                ),
            )

        # 7. General indoor electrical contractor (strictly excluded)
        elif "sparky-electric" in u:
            return CrawlResult(
                domain="sparky-electric.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text=(
                    "Sparky Electrical Services. Licensed electricians providing electrical repairs, "
                    "breaker panel upgrades, ceiling fan installation, and recessed lighting installation. "
                    "We install light fixtures and circuit breakers."
                ),
            )

        # 8. National Big-Box retailer (fast exclusion)
        elif "homedepot" in u:
            return CrawlResult(
                domain="homedepot.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text="Shop Christmas lights, trees, holiday decorations at The Home Depot.",
            )

        # 9. Inaccessible / unreachable site
        elif "dead-domain" in u:
            return CrawlResult(
                domain="dead-domain.com",
                base_url=base_url,
                is_accessible=False,
                status_category="Connection Error",
                error_message="Host name resolution failure",
            )

        # 10. Uncertain / minimal content
        else:
            return CrawlResult(
                domain="sparse-site.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text="Welcome to our website. Coming soon in 2027.",
            )


def test_standard_output_csv_filenames_and_segregation(tmp_path: Path):
    """Verify that the enricher generates all 4 requested CSV files with exact names and correct leads."""
    in_csv = tmp_path / "raw_leads.csv"
    headers = [
        "query",
        "name",
        "category",
        "phone",
        "website",
        "Clean Website Url",
        "Company name",
        "city",
        "state",
    ]
    rows = [
        # Lead 1: Pure Christmas lighting
        ["christmas light installation", "NOW THATS CHRISTMAS", "Service", "111-222-3333", "https://pure-christmas.com", "https://pure-christmas.com", "Now Thats Christmas", "Denver", "CO"],
        # Lead 2: Multi-trade landscaper with seasonal holiday lighting
        ["christmas light installation", "GREEN ACRES LANDSCAPING", "Service", "222-333-4444", "https://green-acres-landscaping.com", "https://green-acres-landscaping.com", "Green Acres Landscaping", "Dallas", "TX"],
        # Lead 3: Window cleaner with seasonal holiday lighting
        ["christmas light installation", "CRYSTAL CLEAR WINDOWS", "Service", "333-444-5555", "https://crystal-clear-windows.com", "https://crystal-clear-windows.com", "Crystal Clear Windows", "Seattle", "WA"],
        # Lead 4: Landscaping only (no holiday lights)
        ["outdoor lighting", "QUALITY LAWN & LIGHT", "Service", "444-555-6666", "https://pure-landscaping-only.com", "https://pure-landscaping-only.com", "Quality Lawn", "Chicago", "IL"],
        # Lead 5: Casual holiday greeting landscaper
        ["landscaping", "OAK VALLEY LANDSCAPING", "Service", "555-666-7777", "https://holiday-hours-landscaper.com", "https://holiday-hours-landscaper.com", "Oak Valley Landscaping", "Austin", "TX"],
        # Lead 6: DJ / Event lighting
        ["event lighting", "ELITE DJ & PRODUCTIONS", "Service", "666-777-8888", "https://sound-and-stage-lighting.com", "https://sound-and-stage-lighting.com", "Elite DJ", "Miami", "FL"],
        # Lead 7: Indoor electrician
        ["electrician", "SPARKY ELECTRICAL", "Service", "777-888-9999", "https://sparky-electric.com", "https://sparky-electric.com", "Sparky Electric", "Boston", "MA"],
        # Lead 8: Home Depot retailer
        ["christmas lights", "THE HOME DEPOT", "Retail", "888-999-0000", "https://homedepot.com/b/Lighting", "https://homedepot.com", "The Home Depot", "Atlanta", "GA"],
        # Lead 9: Dead link
        ["christmas light installation", "DEAD LINK PROS", "Service", "999-000-1111", "https://dead-domain.com", "https://dead-domain.com", "Dead Link Pros", "Phoenix", "AZ"],
        # Lead 10: Uncertain minimal site
        ["christmas light installation", "SPARSE BUSINESS", "Service", "000-111-2222", "https://sparse-site.com", "https://sparse-site.com", "Sparse Business", "Portland", "OR"],
    ]

    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    out_dir = tmp_path / "output"
    enricher = ChristmasLightLeadEnricher(crawler=ComprehensiveMockCrawler(), concurrency_limit=4)

    import asyncio
    telemetry = asyncio.run(
        enricher.process_leads(
            input_csv_path=in_csv,
            output_dir=out_dir,
            resume=False,
        )
    )

    # 1. Check exact required filenames exist in output directory
    qualified_csv = out_dir / "Qualified Leads.csv"
    not_qualified_csv = out_dir / "Not Qualified Leads.csv"
    unreachable_csv = out_dir / "Unreachable Leads.csv"
    uncertain_csv = out_dir / "Uncertain.csv"
    all_csv = out_dir / "All Leads.csv"

    assert qualified_csv.exists(), "Qualified Leads.csv must exist"
    assert not_qualified_csv.exists(), "Not Qualified Leads.csv must exist"
    assert unreachable_csv.exists(), "Unreachable Leads.csv must exist"
    assert uncertain_csv.exists(), "Uncertain.csv must exist"
    assert all_csv.exists(), "All Leads.csv must exist"

    # 2. Check counts
    # Leads 1, 2, 3 should be Qualified
    assert telemetry.qualified_count == 3
    # Leads 4, 5, 6, 7, 8 should be Not Qualified
    assert telemetry.not_qualified_count == 5
    # Lead 9 should be Unreachable
    assert telemetry.unreachable_count == 1
    # Lead 10 should be Uncertain
    assert telemetry.uncertain_count == 1

    # 3. Read Qualified Leads.csv and verify contents
    with open(qualified_csv, encoding="utf-8") as f:
        reader = list(csv.reader(f))
        headers_read = reader[0]
        assert "Does this company provide Christmas light installation?" in headers_read
        assert len(reader) == 4  # header + 3 qualified leads

        names_qualified = [row[headers_read.index("Company name")] for row in reader[1:]]
        assert "Now Thats Christmas" in names_qualified
        assert "Green Acres Landscaping" in names_qualified
        assert "Crystal Clear Windows" in names_qualified

    # 4. Read Not Qualified Leads.csv and verify exclusions
    with open(not_qualified_csv, encoding="utf-8") as f:
        reader = list(csv.reader(f))
        headers_read = reader[0]
        names_excluded = [row[headers_read.index("Company name")] for row in reader[1:]]
        assert "Quality Lawn" in names_excluded
        assert "Oak Valley Landscaping" in names_excluded
        assert "Elite DJ" in names_excluded
        assert "Sparky Electric" in names_excluded
        assert "The Home Depot" in names_excluded

    # 5. Read Unreachable Leads.csv
    with open(unreachable_csv, encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 2  # header + 1 unreachable
        assert reader[1][headers_read.index("Company name")] == "Dead Link Pros"

    # 6. Read Uncertain.csv
    with open(uncertain_csv, encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 2  # header + 1 uncertain
        assert reader[1][headers_read.index("Company name")] == "Sparse Business"


def test_subpath_url_extraction(tmp_path: Path):
    """Verify that a specific branch URL in the 'website' column is prioritized over root 'Clean Website Url'."""
    in_csv = tmp_path / "subpath_leads.csv"
    headers = ["name", "website", "Clean Website Url", "Company name"]
    rows = [
        ["Wonderly Morristown", "https://wonderlylights.com/morristown-nj/", "https://wonderlylights.com", "Wonderly Lights Morristown"],
    ]
    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    crawled_urls = []

    class SubpathSpyCrawler(WebsiteCrawler):
        async def crawl_domain(self, base_url: str) -> CrawlResult:
            crawled_urls.append(base_url)
            return CrawlResult(
                domain="wonderlylights.com",
                base_url=base_url,
                is_accessible=True,
                status_category="OK",
                combined_text="professional christmas light hanging and holiday lighting displays free quote",
            )

    enricher = ChristmasLightLeadEnricher(crawler=SubpathSpyCrawler(), concurrency_limit=1)

    import asyncio
    asyncio.run(enricher.process_leads(input_csv_path=in_csv, output_dir=tmp_path / "out", resume=False))

    # Must crawl the specific location page first
    assert len(crawled_urls) == 1
    assert "morristown-nj" in crawled_urls[0]
