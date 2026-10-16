import asyncio
import csv
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from contractor_scraper.classification.pest_control_service_classifier import (
    PestControlClassificationResult,
    PestControlServiceClassifier,
)
from contractor_scraper.core.enums import WebsiteStatus
from contractor_scraper.discovery.base import WebsiteCandidate, WebsiteVerificationResult
from contractor_scraper.discovery.company_website_matcher import (
    CompanyWebsiteMatcher,
    CompanyWebsiteMatchResult,
)
from contractor_scraper.discovery.crawler import CrawledPage, CrawlResult
from contractor_scraper.orchestration.csv_enricher import CSVEnricher


# ==============================================================================
# 1. Classification Tests
# ==============================================================================


def test_classifier_qualifies_residential_commercial_contractor() -> None:
    """Active pest contractor with technician operations and treatment services is marked 'Yes'."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="apexpestcontrol.com",
        base_url="https://apexpestcontrol.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://apexpestcontrol.com",
            status_code=200,
            title="Apex Pest Control | Residential & Commercial Exterminators",
            meta_description="Professional pest control services in Tampa, FL. Call today!",
            text_content="We provide residential pest control and commercial pest control services.",
        ),
        subpages=[
            CrawledPage(
                url="https://apexpestcontrol.com/services",
                status_code=200,
                title="Our Pest Services",
                text_content=(
                    "Our licensed technicians offer comprehensive termite treatment, "
                    "bed bug treatment, and rodent control. Schedule an inspection today "
                    "for our perimeter pest control treatment plans."
                ),
            )
        ],
        combined_text=(
            "Apex Pest Control Tampa. Residential pest control, commercial pest control, "
            "termite inspection and termite treatment. Our licensed technicians protect your home. "
            "Schedule an inspection today for family owned pest management services."
        ),
    )

    result = classifier.evaluate_crawl(crawl, company_name="Apex Pest Control")
    assert result.is_pest_control is True
    assert result.decision_label == "Yes"
    assert result.confidence >= 0.80
    assert any("residential pest control" in p for p in result.evidence_phrases)
    assert any("technician" in s for s in result.operational_signals)


def test_classifier_qualifies_exterminator_with_multiple_treatments() -> None:
    """Exterminator with termite, bed bug, and rodent treatments qualifies."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="flterminate.com",
        base_url="https://flterminate.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://flterminate.com",
            status_code=200,
            title="Florida Exterminating Services",
            text_content="Termite control, bed bug extermination, and rodent exclusion.",
        ),
        subpages=[],
        combined_text=(
            "We offer termite treatment, bed bug extermination, and rodent control. "
            "Our pest professionals serve Central Florida with guaranteed results. "
            "Schedule service today."
        ),
    )

    result = classifier.evaluate_crawl(crawl)
    assert result.is_pest_control is True
    assert result.decision_label == "Yes"


def test_classifier_disqualifies_ecommerce_store() -> None:
    """E-commerce DIY pest control store with shopping cart is marked 'No'."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="diypestsupplies.com",
        base_url="https://diypestsupplies.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://diypestsupplies.com",
            status_code=200,
            title="DIY Pest Control Products & Chemicals Store",
            text_content="Buy professional pest control products online. Free shipping on orders over $50.",
        ),
        subpages=[
            CrawledPage(
                url="https://diypestsupplies.com/cart",
                status_code=200,
                title="Your Cart",
                text_content="Add to cart. Proceed to checkout. Sprayers and insecticide concentrates.",
            )
        ],
        combined_text=(
            "Buy pest control products for sale. Add to cart. View cart. Proceed to checkout. "
            "DIY pest control chemical distributor. Free shipping on orders."
        ),
    )

    result = classifier.evaluate_crawl(crawl)
    assert result.is_pest_control is False
    assert result.decision_label == "No"
    assert any("cart" in d or "checkout" in d for d in result.disqualifiers)


def test_classifier_disqualifies_software_company() -> None:
    """Pest control CRM / SaaS software provider is marked 'No'."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="pestbosssoftware.com",
        base_url="https://pestbosssoftware.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://pestbosssoftware.com",
            status_code=200,
            title="PestBoss - Field Service Pest Control Software",
            text_content="The #1 pest control software for exterminators. Book a demo today.",
        ),
        combined_text=(
            "All-in-one pest control software and pest control crm. "
            "Field service management software with routing and scheduling software. "
            "Book a demo. Pricing per technician per month."
        ),
    )

    result = classifier.evaluate_crawl(crawl)
    assert result.is_pest_control is False
    assert result.decision_label == "No"
    assert "software" in result.reasoning.lower() or "demo" in result.reasoning.lower()


def test_classifier_disqualifies_directory_portal() -> None:
    """Lead aggregator / directory portal is marked 'No'."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="bestexterminatorquotes.com",
        base_url="https://bestexterminatorquotes.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://bestexterminatorquotes.com",
            status_code=200,
            title="Find Local Exterminators Near You",
            text_content="Compare pest control quotes from top 10 exterminators.",
        ),
        combined_text=(
            "Find local exterminators near you. Directory of exterminators. "
            "Compare pest control quotes. Top 10 pest control companies."
        ),
    )

    result = classifier.evaluate_crawl(crawl)
    assert result.is_pest_control is False
    assert result.decision_label == "No"


def test_classifier_disqualifies_unrelated_business_with_pest_mention() -> None:
    """Unrelated company (e.g. general cleaner or car wash) mentioning pest is marked 'No'."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="supercarpetcleaners.com",
        base_url="https://supercarpetcleaners.com",
        is_accessible=True,
        homepage=CrawledPage(
            url="https://supercarpetcleaners.com",
            status_code=200,
            title="Super Carpet Cleaners Orlando",
            text_content="Steam carpet cleaning, tile scrubbing, and upholstery care.",
        ),
        combined_text=(
            "Orlando carpet cleaning pros. We steam clean rugs and remove stains. "
            "We occasionally sweep up dead bugs."
        ),
    )

    result = classifier.evaluate_crawl(crawl)
    assert result.is_pest_control is False
    assert result.decision_label == "No"


def test_classifier_inaccessible_website() -> None:
    """Inaccessible site returns 'No'."""
    classifier = PestControlServiceClassifier()
    crawl = CrawlResult(
        domain="brokensite123.com",
        base_url="https://brokensite123.com",
        is_accessible=False,
        error_message="DNS resolution failed",
    )

    result = classifier.evaluate_crawl(crawl)
    assert result.is_pest_control is False
    assert result.decision_label == "No"


# ==============================================================================
# 2. CompanyWebsiteMatcher Tests
# ==============================================================================


def test_matcher_verifies_with_phone_and_qualifies_service() -> None:
    """Matcher discovers candidate, verifies via matching phone number, and qualifies service."""
    mock_finder = MagicMock()
    mock_finder.find_websites = AsyncMock(
        return_value=[
            WebsiteCandidate(
                url="https://www.007exterminators.com",
                domain="007exterminators.com",
                discovery_source="public_search",
                initial_confidence=0.80,
            )
        ]
    )

    mock_crawler = MagicMock()
    mock_crawler.crawl_domain = AsyncMock(
        return_value=CrawlResult(
            domain="007exterminators.com",
            base_url="https://www.007exterminators.com",
            is_accessible=True,
            all_phones=["(813) 875-9070"],
            homepage=CrawledPage(
                url="https://www.007exterminators.com",
                status_code=200,
                title="007 Exterminators Tampa | Pest & Termite Control",
                text_content="Tampa pest control and fumigation services. Call 813-875-9070.",
            ),
            combined_text=(
                "007 Exterminators Inc in Tampa FL. Residential pest control, "
                "tent fumigation, termite treatment, and rodent control. "
                "Our licensed technicians protect Tampa homes. Schedule an inspection at 813-875-9070."
            ),
        )
    )

    matcher = CompanyWebsiteMatcher(finder=mock_finder, crawler=mock_crawler)
    result = asyncio.run(
        matcher.match_company_website(
            company_name="007 EXTERMINATORS INC",
            city="Tampa",
            state="FL",
            phone="813-875-9070",
            address="4412 N LOIS AVE\nTAMPA 33614",
        )
    )

    assert result.website == "https://www.007exterminators.com"
    assert result.domain == "007exterminators.com"
    assert result.is_pest_control is True
    assert result.decision_label == "Yes"
    assert "phone_number_match" in result.verification_checks


def test_matcher_leaves_empty_when_no_match() -> None:
    """When no candidates meet verification threshold, website is empty and classified 'No'."""
    mock_finder = MagicMock()
    mock_finder.find_websites = AsyncMock(return_value=[])

    matcher = CompanyWebsiteMatcher(finder=mock_finder)
    result = asyncio.run(
        matcher.match_company_website(
            company_name="Ghost Pest Solutions",
            city="Nowhere",
            state="FL",
        )
    )

    assert result.website == ""
    assert result.is_pest_control is False
    assert result.decision_label == "No"


# ==============================================================================
# 3. CSVEnricher Tests
# ==============================================================================


def test_csv_enricher_preserves_structure_and_duplicate_columns(tmp_path: Path) -> None:
    """Verifies that all 43 original columns, duplicate headers, emails, and multiline addresses are preserved."""
    input_file = tmp_path / "test_raw_leads.csv"
    output_file = tmp_path / "test_enriched_leads.csv"

    # CSV headers modeled exactly on user's dataset (including duplicate 'email', 'status', and empty col '')
    headers = [
        "dba_name",
        "industry",
        "",
        "First Name",
        "company_name",
        "email",
        "Is Pest Control Company ?",
        "Company Having Website ?",
        "city",
        "state",
        "phone",
        "address",
        "addr_state",
        "zip",
        "county",
        "license_number",
        "license_class",
        "status",
        "record_level",
        "applicator_count",
        "source_note",
        "dupe_group_size",
        "is_primary_record",
        "email",  # duplicate email header
        "username",
        "domain",
        "status",  # duplicate status header
        "overall_score",
        "is_safe_to_send",
        "is_valid_syntax",
        "is_disposable",
        "is_role_account",
        "mx_accepts_mail",
        "mx_records",
        "can_connect_smtp",
        "has_inbox_full",
        "is_catch_all",
        "is_deliverable",
        "is_disabled",
        "is_spamtrap",
        "is_free_email",
        "is_forwarded",
        "Pest Keyword",
    ]

    row_1 = [
        "",
        "structural pest control",
        "",
        "",
        "Apex Pest Solutions",
        "apex@aol.com",  # Email must NEVER be modified
        "",  # Is Pest Control Company ? (initially empty)
        "",  # Company Having Website ? (initially empty)
        "Tampa",
        "FL",
        "813-555-0101",
        "123 Main St\nTampa 33601",  # multiline address
        "FL",
        "33601",
        "Hillsborough",
        "JB1001",
        "General Household Pest",
        "Active",
        "business",
        "",
        "State extract",
        "1",
        "TRUE",
        "apex@aol.com",  # duplicate email column
        "apex",
        "aol.com",
        "safe",  # duplicate status column
        "85",
        "TRUE",
        "TRUE",
        "FALSE",
        "FALSE",
        "TRUE",
        "mx.aol.com",
        "TRUE",
        "FALSE",
        "FALSE",
        "TRUE",
        "FALSE",
        "FALSE",
        "TRUE",
        "FALSE",
        "✓",  # Pest Keyword checkmark
    ]

    row_2 = [
        "",
        "structural pest control",
        "",
        "",
        "Unregistered Unknown Bug Guy",
        "bugguy@gmail.com",
        "",
        "",
        "Miami",
        "FL",
        "305-555-9999",
        "456 South Rd\nMiami 33101",
        "FL",
        "33101",
        "Dade",
        "JB9999",
        "General Household Pest",
        "Active",
        "business",
        "",
        "State extract",
        "1",
        "TRUE",
        "bugguy@gmail.com",
        "bugguy",
        "gmail.com",
        "safe",
        "90",
        "TRUE",
        "TRUE",
        "FALSE",
        "FALSE",
        "TRUE",
        "mx.google.com",
        "TRUE",
        "FALSE",
        "FALSE",
        "TRUE",
        "FALSE",
        "FALSE",
        "TRUE",
        "FALSE",
        "✓",
    ]

    with open(input_file, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(headers)
        writer.writerow(row_1)
        writer.writerow(row_2)

    # Mock Matcher to return deterministic outputs
    mock_matcher = MagicMock()

    async def mock_match_side_effect(company_name: str, **kwargs: str) -> CompanyWebsiteMatchResult:
        if "Apex" in company_name:
            return CompanyWebsiteMatchResult(
                website="https://www.apexpest.com",
                domain="apexpest.com",
                is_pest_control=True,
                decision_label="Yes",
                confidence=0.92,
                verification_status="VERIFIED",
                reasoning="Verified residential pest contractor",
            )
        else:
            return CompanyWebsiteMatchResult(
                website="",
                domain="",
                is_pest_control=False,
                decision_label="No",
                confidence=0.0,
                verification_status="NOT_FOUND",
                reasoning="No website found",
            )

    mock_matcher.match_company_website = AsyncMock(side_effect=mock_match_side_effect)

    enricher = CSVEnricher(matcher=mock_matcher, concurrency_limit=2)
    telemetry = asyncio.run(
        enricher.enrich_csv(
            input_csv_path=input_file,
            output_csv_path=output_file,
            resume=False,
        )
    )

    assert telemetry.total_records == 2
    assert telemetry.records_processed == 2
    assert telemetry.websites_discovered == 1
    assert telemetry.pest_control_yes == 1
    assert telemetry.pest_control_no == 1

    # Verify output file
    with open(output_file, mode="r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        out_headers = next(reader)
        out_rows = list(reader)

    # Verify 100% header match, including duplicate column names!
    assert out_headers == headers
    assert len(out_rows) == 2

    # Row 1 verification
    out_row_1 = out_rows[0]
    web_idx = headers.index("Company Having Website ?")
    pest_idx = headers.index("Is Pest Control Company ?")

    assert out_row_1[web_idx] == "https://www.apexpest.com"
    assert out_row_1[pest_idx] == "Yes"
    # Verify email was NOT touched
    assert out_row_1[5] == "apex@aol.com"
    assert out_row_1[23] == "apex@aol.com"
    # Verify multiline address intact
    assert "123 Main St\nTampa 33601" in out_row_1[11]
    # Verify unicode checkmark intact
    assert out_row_1[42] == "✓"

    # Row 2 verification
    out_row_2 = out_rows[1]
    assert out_row_2[web_idx] == ""
    assert out_row_2[pest_idx] == "No"
    assert out_row_2[5] == "bugguy@gmail.com"


def test_matcher_reuses_existing_valid_website() -> None:
    """When a valid website is already present in the CSV record, it is verified and reused."""
    mock_finder = MagicMock()
    mock_finder.find_websites = AsyncMock(return_value=[])

    mock_crawler = MagicMock()
    mock_crawler.crawl_domain = AsyncMock(
        return_value=CrawlResult(
            domain="existingpestpro.com",
            base_url="https://existingpestpro.com",
            is_accessible=True,
            homepage=CrawledPage(
                url="https://existingpestpro.com",
                status_code=200,
                title="Existing Pest Pro | Pest Control Contractor",
                text_content="Serving Orlando FL with termite treatments and pest management.",
            ),
            combined_text=(
                "Existing Pest Pro Orlando Florida. Residential pest control services, "
                "termite treatment, and rodent control. Our technicians provide free inspections."
            ),
        )
    )

    matcher = CompanyWebsiteMatcher(finder=mock_finder, crawler=mock_crawler)
    result = asyncio.run(
        matcher.match_company_website(
            company_name="Existing Pest Pro",
            city="Orlando",
            state="FL",
            existing_website="https://existingpestpro.com",
        )
    )

    assert result.website == "https://existingpestpro.com"
    assert result.is_pest_control is True
    assert result.decision_label == "Yes"
    assert result.verification_status == "VERIFIED_EXISTING"
    # Finder should NOT have been called because existing website was verified directly!
    mock_finder.find_websites.assert_not_called()


def test_csv_enricher_resume_functionality(tmp_path: Path) -> None:
    """When output file already exists with some rows processed, resume=True skips re-processing them."""
    input_file = tmp_path / "resume_input.csv"
    output_file = tmp_path / "resume_output.csv"

    headers = ["company_name", "city", "state", "Company Having Website ?", "Is Pest Control Company ?"]
    rows = [
        ["Row 1 Already Done", "Tampa", "FL", "https://done1.com", "Yes"],
        ["Row 2 Needs Processing", "Miami", "FL", "", ""],
    ]

    with open(input_file, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    # Pre-populate output file with row 1 already done
    with open(output_file, mode="w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerow(rows[0])
        writer.writerow(["Row 2 Needs Processing", "Miami", "FL", "", ""])

    mock_matcher = MagicMock()
    mock_matcher.match_company_website = AsyncMock(
        return_value=CompanyWebsiteMatchResult(
            website="https://row2found.com",
            domain="row2found.com",
            is_pest_control=True,
            decision_label="Yes",
            confidence=0.9,
            verification_status="VERIFIED",
            reasoning="Found row 2",
        )
    )

    enricher = CSVEnricher(matcher=mock_matcher, concurrency_limit=1)
    telemetry = asyncio.run(
        enricher.enrich_csv(
            input_csv_path=input_file,
            output_csv_path=output_file,
            resume=True,
        )
    )

    assert telemetry.records_processed == 2
    assert telemetry.websites_discovered == 2
    # Matcher was only called for Row 2, Row 1 was resumed!
    assert mock_matcher.match_company_website.call_count == 1
