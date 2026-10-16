"""Integration tests for the ChristmasLightLeadEnricher pipeline."""

import csv
from pathlib import Path
import pytest

from contractor_scraper.classification.christmas_light_classifier import (
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler
from contractor_scraper.orchestration.christmas_light_enricher import ChristmasLightLeadEnricher


class MockCrawler(WebsiteCrawler):
    """Mock crawler that returns deterministic responses without network calls."""

    async def crawl_domain(self, base_url: str) -> CrawlResult:
        u = base_url.lower()
        if "qualified-installer" in u:
            return CrawlResult(
                domain="qualified-installer.com",
                base_url=base_url,
                is_accessible=True,
                combined_text="residential christmas light installation commercial holiday lighting free quote our installers takedown and storage",
                status_category="OK",
            )
        elif "shop-retail" in u:
            return CrawlResult(
                domain="shop-retail.com",
                base_url=base_url,
                is_accessible=True,
                combined_text="add to cart view cart proceed to checkout buy online christmas lights product catalog",
                status_category="OK",
            )
        elif "uncertain-site" in u:
            return CrawlResult(
                domain="uncertain-site.com",
                base_url=base_url,
                is_accessible=True,
                combined_text="hello world coming soon under construction",
                status_category="OK",
            )
        elif "unreachable-site" in u:
            return CrawlResult(
                domain="unreachable-site.com",
                base_url=base_url,
                is_accessible=False,
                error_message="Connection timed out",
                status_category="Timeout",
            )
        else:
            return CrawlResult(
                domain="other.com",
                base_url=base_url,
                is_accessible=False,
                error_message="Host unreachable",
                status_category="Connection Error",
            )


def test_enricher_pipeline_outputs(tmp_path: Path):
    # 1. Create a sample input CSV
    in_csv = tmp_path / "test_input.csv"
    headers = [
        "Company name",
        "Email",
        "Website",
        "Clean Website URL's",
        "City",
        "State",
        "Phone",
    ]
    sample_rows = [
        ["Holiday Pros LLC", "info@holidaypros.com", "https://qualified-installer.com", "https://qualified-installer.com", "Atlanta", "GA", "404-123-4567"],
        ["Light Bulb Superstore", "sales@shopretail.com", "https://shop-retail.com", "https://shop-retail.com", "Orlando", "FL", "407-123-4567"],
        ["Vague Business", "hello@vague.com", "https://uncertain-site.com", "https://uncertain-site.com", "Miami", "FL", "305-123-4567"],
        ["Dead Link Co", "contact@dead.com", "https://unreachable-site.com", "https://unreachable-site.com", "Tampa", "FL", "813-123-4567"],
    ]

    with open(in_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(sample_rows)

    out_dir = tmp_path / "output"
    enricher = ChristmasLightLeadEnricher(crawler=MockCrawler(), concurrency_limit=2)

    import asyncio
    telemetry = asyncio.run(
        enricher.process_leads(
            input_csv_path=in_csv,
            output_dir=out_dir,
            resume=False,
        )
    )

    assert telemetry.total_records == 4
    assert telemetry.records_processed == 4
    assert telemetry.qualified_count == 1
    assert telemetry.not_qualified_count == 1
    assert telemetry.uncertain_count == 1
    assert telemetry.unreachable_count == 1

    # Verify all 4 required CSV files were generated
    qualified_file = out_dir / "christmas_installation_qualified.csv"
    not_qualified_file = out_dir / "christmas_installation_not_qualified.csv"
    uncertain_file = out_dir / "christmas_installation_uncertain.csv"
    unreachable_file = out_dir / "christmas_installation_unreachable.csv"
    all_file = out_dir / "christmas_installation_all.csv"

    assert qualified_file.exists()
    assert not_qualified_file.exists()
    assert uncertain_file.exists()
    assert unreachable_file.exists()
    assert all_file.exists()

    # Read qualified file
    with open(qualified_file, encoding="utf-8") as f:
        r = list(csv.reader(f))
        header_row = r[0]
        assert "Does this company provide Christmas light installation?" in header_row
        assert len(r) == 2  # header + 1 qualified lead
        assert r[1][0] == "Holiday Pros LLC"
        c_idx = header_row.index("Does this company provide Christmas light installation?")
        assert r[1][c_idx] == "Yes"

    # Read not qualified file
    with open(not_qualified_file, encoding="utf-8") as f:
        r = list(csv.reader(f))
        assert len(r) == 2
        assert r[1][0] == "Light Bulb Superstore"
        c_idx = r[0].index("Does this company provide Christmas light installation?")
        assert r[1][c_idx] == "No"

    # Read uncertain file
    with open(uncertain_file, encoding="utf-8") as f:
        r = list(csv.reader(f))
        assert len(r) == 2
        assert r[1][0] == "Vague Business"
        c_idx = r[0].index("Does this company provide Christmas light installation?")
        assert r[1][c_idx] == "Uncertain"

    # Read unreachable file
    with open(unreachable_file, encoding="utf-8") as f:
        r = list(csv.reader(f))
        assert len(r) == 2
        assert r[1][0] == "Dead Link Co"
        status_idx = r[0].index("Website status")
        assert r[1][status_idx] == "Timeout"
