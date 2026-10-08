"""Tests for end-to-end LeadEngine execution, limit behavior, and CSV output integrity."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import polars as pl

from contractor_scraper.discovery.base import WebsiteCandidate, WebsiteVerificationResult
from contractor_scraper.discovery.crawler import CrawlResult
from contractor_scraper.extraction.base import EmailCandidate, EmailVerificationResult
from contractor_scraper.orchestration.lead_engine import LeadEngine
from tests.conftest import make_sunbiz_line


def test_lead_engine_end_to_end_mocked(tmp_path: Path) -> None:
    """Verify end-to-end LeadEngine run parses Sunbiz records, discovers/verifies contact info, and exports valid CSV."""

    async def _test() -> None:
        engine = LeadEngine(output_dir=tmp_path)

        # 1. Mock Sunbiz raw streaming lines
        mock_lines = [
            # Match 1: Valid roofing contractor
            make_sunbiz_line(
                corp_num="P2500001", name="APEX ROOFING LLC", city="ORLANDO", file_date="01012025"
            ),
            # Match 2: Valid roofing contractor
            make_sunbiz_line(
                corp_num="P2500002",
                name="SUMMIT ROOF REPAIRS INC",
                city="TAMPA",
                file_date="01022025",
            ),
            # Match 3: Unrelated company (excluded by classifier)
            make_sunbiz_line(
                corp_num="P2500003",
                name="GOLDEN SUN FINANCIAL SERVICES LLC",
                city="MIAMI",
                file_date="01032025",
            ),
            # Match 4: Roofing supply (excluded by negative rules)
            make_sunbiz_line(
                corp_num="P2500004",
                name="ROOFING SUPPLY & WHOLESALE MATERIALS CORP",
                city="ORLANDO",
                file_date="01042025",
            ),
            # Match 5: Valid roofing contractor
            make_sunbiz_line(
                corp_num="P2500005",
                name="COASTAL ROOFING PROS LLC",
                city="JACKSONVILLE",
                file_date="01052025",
            ),
        ]

        engine.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter(
            mock_lines
        )

        # 2. Mock WebsiteFinder to return candidates for company 1 and 2, but None for company 5
        async def mock_find_websites(company_name, city, state="FL", category="roofing"):
            if "Apex" in company_name:
                return [
                    WebsiteCandidate(
                        url="https://apexroofingfl.com",
                        domain="apexroofingfl.com",
                        discovery_source="mock",
                        initial_confidence=0.9,
                    )
                ]
            elif "Summit" in company_name:
                return [
                    WebsiteCandidate(
                        url="https://summitroofs.com",
                        domain="summitroofs.com",
                        discovery_source="mock",
                        initial_confidence=0.9,
                    )
                ]
            return []

        engine.finder.find_websites = AsyncMock(side_effect=mock_find_websites)

        # 3. Mock WebsiteVerifier to verify candidate
        async def mock_verify_website(
            candidate,
            expected_company_name,
            expected_state="FL",
            expected_city=None,
            category="roofing",
        ):
            return WebsiteVerificationResult(
                url=candidate.url,
                domain=candidate.domain,
                status="VERIFIED",
                is_verified=True,
                confidence=0.92,
                verification_checks=["state_fl_match", "city_match"],
            )

        engine.verifier.verify_website = AsyncMock(side_effect=mock_verify_website)

        # 4. Mock Crawler
        async def mock_crawl_domain(url):
            return CrawlResult(
                domain="mock.com",
                base_url=url,
                is_accessible=True,
                all_emails=[
                    "info@apexroofingfl.com" if "apex" in url else "contact@summitroofs.com"
                ],
            )

        engine.crawler.crawl_domain = AsyncMock(side_effect=mock_crawl_domain)

        # 5. Mock EmailExtractor
        async def mock_extract_emails(url, crawl_result=None):
            addr = "info@apexroofingfl.com" if "apex" in url else "contact@summitroofs.com"
            return [EmailCandidate(email_address=addr, source_url=url, initial_confidence=0.95)]

        engine.email_extractor.extract_emails = AsyncMock(side_effect=mock_extract_emails)

        # 6. Mock EmailVerifier
        async def mock_verify_email(candidate, expected_domain=None):
            return EmailVerificationResult(
                email_address=candidate.email_address,
                status="DELIVERABLE",
                is_verified=True,
                confidence=0.95,
            )

        engine.email_verifier.verify_email = AsyncMock(side_effect=mock_verify_email)

        # Run engine with limit=10
        csv_path, telemetry = await engine.run(year=2025, category="roofing", limit=10)

        # Verify CSV file existence
        assert csv_path.exists()
        assert csv_path.stat().st_size > 0

        # Verify exact CSV columns using Polars
        df = pl.read_csv(csv_path)
        expected_cols = [
            "Company Name",
            "Website",
            "Email",
            "City",
            "State",
            "Formation Date",
            "Document Number",
            "Company Status",
            "Street Address",
            "ZIP Code",
            "Industry",
        ]
        assert df.columns == expected_cols

        # Exactly 2 complete leads should have been created (Match 1 and Match 2)
        assert len(df) == 2
        assert telemetry.final_complete_leads == 2
        assert telemetry.raw_records_examined == 5
        assert telemetry.category_candidates == 3  # Apex, Summit, Coastal (Supply was excluded)

        # Verify core identity fields are populated
        for row in df.iter_rows(named=True):
            for col in ["Company Name", "Website", "City", "State"]:
                val = row[col]
                assert val is not None and str(val).strip() != "", (
                    f"Column '{col}' must not be blank"
                )
                assert val not in {"N/A", "Unknown", "None"}

        # Verify specific data values
        names = df["Company Name"].to_list()
        assert "Apex Roofing" in names
        assert "Summit Roof Repairs" in names

        # Verify dual CSV output files in output directory
        prod_csv = tmp_path / "production_leads.csv"
        missing_csv = tmp_path / "leads_missing_contact.csv"
        assert prod_csv.exists()
        assert missing_csv.exists()

        missing_df = pl.read_csv(missing_csv)
        assert len(missing_df) == 1
        assert missing_df["Company Name"].to_list() == ["Coastal Roofing Pros"]
        assert missing_df["City"].to_list() == ["Jacksonville"]
        assert missing_df["Industry"].to_list() == ["Roofing"]

        # Neither file should contain companies excluded by the classifier
        assert "GOLDEN SUN FINANCIAL SERVICES" not in names
        assert "ROOFING SUPPLY & WHOLESALE MATERIALS" not in names
        missing_names = missing_df["Company Name"].to_list()
        assert "GOLDEN SUN FINANCIAL SERVICES" not in missing_names
        assert "ROOFING SUPPLY & WHOLESALE MATERIALS" not in missing_names

    asyncio.run(_test())


def test_lead_engine_limit_behavior(tmp_path: Path) -> None:
    """Verify LeadEngine stops processing as soon as requested limit of verified complete leads is reached."""

    async def _test() -> None:
        engine = LeadEngine(output_dir=tmp_path)

        # 5 matching roofing records
        mock_lines = [
            make_sunbiz_line(
                corp_num=f"P250000{i}",
                name=f"ROOFING PRO {i} LLC",
                city="ORLANDO",
                file_date="01012025",
            )
            for i in range(1, 6)
        ]
        engine.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter(
            mock_lines
        )

        # Mock all verifications succeeding
        engine.finder.find_websites = AsyncMock(
            side_effect=lambda company_name, city, state="FL", category="roofing", **kwargs: [
                WebsiteCandidate(
                    url=f"https://{company_name.lower().replace(' ', '')}.com",
                    domain=f"{company_name.lower().replace(' ', '')}.com",
                    discovery_source="mock",
                    initial_confidence=0.9,
                )
            ]
        )
        engine.verifier.verify_website = AsyncMock(
            return_value=WebsiteVerificationResult(
                url="https://site.com",
                domain="site.com",
                status="VERIFIED",
                is_verified=True,
                confidence=0.9,
            )
        )
        engine.crawler.crawl_domain = AsyncMock(
            return_value=CrawlResult(
                domain="site.com",
                base_url="https://site.com",
                is_accessible=True,
                all_emails=["info@site.com"],
            )
        )
        engine.email_extractor.extract_emails = AsyncMock(
            side_effect=lambda url, crawl_result=None, **kwargs: [
                EmailCandidate(
                    email_address=f"info@{url.replace('https://', '')}",
                    source_url=url,
                    initial_confidence=0.95,
                )
            ]
        )
        engine.email_verifier.verify_email = AsyncMock(
            side_effect=lambda candidate, expected_domain=None, **kwargs: EmailVerificationResult(
                email_address=candidate.email_address,
                status="DELIVERABLE",
                is_verified=True,
                confidence=0.95,
            )
        )

        # Request limit of 2 leads
        csv_path, telemetry = await engine.run(year=2025, category="roofing", limit=2)

        df = pl.read_csv(csv_path)
        assert len(df) == 2
        assert telemetry.final_complete_leads == 2

    asyncio.run(_test())


def test_lead_engine_dual_csv_med_spa_strict_filtering(tmp_path: Path) -> None:
    """Verify dual CSV export and strict filtering on 'med-spa' category."""

    async def _test() -> None:
        engine = LeadEngine(output_dir=tmp_path)

        mock_lines = [
            # 1. Genuine med spa with website and email
            make_sunbiz_line(
                corp_num="P2500010",
                name="GLOW MED SPA LLC",
                city="ORLANDO",
                file_date="01012025",
            ),
            # 2. Genuine med spa without website/email
            make_sunbiz_line(
                corp_num="P2500020",
                name="RADIANCE MEDICAL SPA INC",
                city="TAMPA",
                file_date="01022025",
            ),
            # 3. Unrelated company (hardware store)
            make_sunbiz_line(
                corp_num="P2500030",
                name="ACME HARDWARE STORE LLC",
                city="MIAMI",
                file_date="01032025",
            ),
            # 4. Tech / software negative exclusion
            make_sunbiz_line(
                corp_num="P2500040",
                name="MED SPA SOFTWARE SOLUTIONS CORP",
                city="ORLANDO",
                file_date="01042025",
            ),
        ]
        engine.sunbiz_source.stream_raw_records_for_year = lambda year, max_files=None: iter(
            mock_lines
        )

        async def mock_find_websites(company_name, city, state="FL", category="med_spa"):
            if "Glow" in company_name:
                return [
                    WebsiteCandidate(
                        url="https://glowmedspaorlando.com",
                        domain="glowmedspaorlando.com",
                        discovery_source="mock",
                        initial_confidence=0.9,
                    )
                ]
            return []

        engine.finder.find_websites = AsyncMock(side_effect=mock_find_websites)
        engine.verifier.verify_website = AsyncMock(
            return_value=WebsiteVerificationResult(
                url="https://glowmedspaorlando.com",
                domain="glowmedspaorlando.com",
                status="VERIFIED",
                is_verified=True,
                confidence=0.92,
            )
        )
        engine.crawler.crawl_domain = AsyncMock(
            return_value=CrawlResult(
                domain="glowmedspaorlando.com",
                base_url="https://glowmedspaorlando.com",
                is_accessible=True,
                all_emails=["contact@glowmedspaorlando.com"],
            )
        )
        engine.email_extractor.extract_emails = AsyncMock(
            return_value=[
                EmailCandidate(
                    email_address="contact@glowmedspaorlando.com",
                    source_url="https://glowmedspaorlando.com",
                    initial_confidence=0.95,
                )
            ]
        )
        engine.email_verifier.verify_email = AsyncMock(
            return_value=EmailVerificationResult(
                email_address="contact@glowmedspaorlando.com",
                status="DELIVERABLE",
                is_verified=True,
                confidence=0.95,
            )
        )

        csv_path, telemetry = await engine.run(year=2025, category="med-spa", limit=10)

        prod_csv = tmp_path / "production_leads.csv"
        missing_csv = tmp_path / "leads_missing_contact.csv"
        assert prod_csv.exists()
        assert missing_csv.exists()

        prod_df = pl.read_csv(prod_csv)
        missing_df = pl.read_csv(missing_csv)

        # 1 production lead (Glow Med Spa)
        assert len(prod_df) == 1
        assert prod_df["Company Name"][0] == "Glow Med Spa"
        assert prod_df["Email"][0] == "contact@glowmedspaorlando.com"
        assert prod_df["Website"][0] == "https://glowmedspaorlando.com"
        assert prod_df["Industry"][0] == "Med Spa"

        # 1 missing contact lead (Radiance Medical Spa)
        assert len(missing_df) == 1
        assert missing_df["Company Name"][0] == "Radiance Medical Spa"
        assert missing_df["City"][0] == "Tampa"
        assert missing_df["Industry"][0] == "Med Spa"
        assert str(missing_df["Email"][0] or "") == ""
        assert str(missing_df["Website"][0] or "") == ""

        # Unrelated companies must not be in either file
        all_exported_names = (
            prod_df["Company Name"].to_list() + missing_df["Company Name"].to_list()
        )
        assert "Acme Hardware Store" not in all_exported_names
        assert "Med Spa Software Solutions" not in all_exported_names

    asyncio.run(_test())
