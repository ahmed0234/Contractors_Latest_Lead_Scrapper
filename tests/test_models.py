"""Tests for domain entity validation and final lead criteria."""

import pytest
from pydantic import ValidationError

from contractor_scraper.core.enums import (
    CompanyStatus,
    ContractorCategory,
    EmailStatus,
    WebsiteStatus,
)
from contractor_scraper.models.company import CompanyCreate
from contractor_scraper.models.email import EmailCreate
from contractor_scraper.models.lead import FinalLead
from contractor_scraper.models.run import EnrichmentAttemptCreate, ProcessingRunCreate
from contractor_scraper.models.website import WebsiteCreate


def test_company_create_valid() -> None:
    """Ensure CompanyCreate initializes valid entity."""
    comp = CompanyCreate(
        name="Reliable Plumbing Co",
        city="Houston",
        state="TX",
        category=ContractorCategory.PLUMBING,
    )
    assert comp.name == "Reliable Plumbing Co"
    assert comp.state == "TX"
    assert comp.status == CompanyStatus.DISCOVERED
    assert comp.id is not None


def test_company_create_invalid_state() -> None:
    """Ensure invalid state code is rejected."""
    with pytest.raises(ValidationError):
        CompanyCreate(
            name="Bad State Co",
            city="Nowhere",
            state="XX",
            category=ContractorCategory.ROOFING,
        )


def test_final_lead_all_fields_valid() -> None:
    """Verify FinalLead serializes all practical lead generation fields."""
    lead = FinalLead(
        company_name="Lone Star Roofing",
        website="https://lonestarroofing.com",
        email="info@lonestarroofing.com",
        city="Dallas",
        state="TX",
        formation_date="2025-01-01",
        document_number="P2500001",
        company_status="Active",
        street_address="123 Main St",
        zip_code="75001",
        industry="Roofing",
    )
    output = lead.to_output_dict()
    assert output == {
        "Company Name": "Lone Star Roofing",
        "Website": "https://lonestarroofing.com",
        "Email": "info@lonestarroofing.com",
        "City": "Dallas",
        "State": "TX",
        "Formation Date": "2025-01-01",
        "Document Number": "P2500001",
        "Company Status": "Active",
        "Street Address": "123 Main St",
        "ZIP Code": "75001",
        "Industry": "Roofing",
    }


def test_final_lead_allows_blank_email() -> None:
    """Email is optional; missing email should be left blank without failing validation."""
    lead = FinalLead(
        company_name="Lone Star Roofing",
        website="https://lonestarroofing.com",
        email="",
        city="Dallas",
        state="TX",
    )
    assert lead.email == ""
    assert lead.company_name == "Lone Star Roofing"


def test_final_lead_missing_company_name_rejected() -> None:
    """Company name is strictly required."""
    with pytest.raises(ValidationError):
        FinalLead(
            company_name="",
            city="Dallas",
            state="TX",
        )


def test_final_lead_normalizes_email_lowercase() -> None:
    """Valid email is automatically lowercased."""
    lead = FinalLead(
        company_name="Lone Star Roofing",
        email="Sales@LoneStarRoofing.com",
        city="Dallas",
        state="TX",
    )
    assert lead.email == "sales@lonestarroofing.com"


def test_website_model_url_normalization() -> None:
    """Verify URL prefix is normalized when missing scheme."""
    site = WebsiteCreate(
        company_id="comp-123",
        url="examplecontractor.com",
        domain="examplecontractor.com",
    )
    assert site.url == "https://examplecontractor.com"
    assert site.verification_status == WebsiteStatus.PENDING


def test_email_model_lowercase_normalization() -> None:
    """Verify email address is lowercased automatically."""
    email = EmailCreate(
        company_id="comp-123",
        email_address="Contact@ContractorPro.COM",
    )
    assert email.email_address == "contact@contractorpro.com"
    assert email.verification_status == EmailStatus.PENDING


def test_processing_run_model() -> None:
    """Verify ProcessingRunCreate instantiates properly."""
    run = ProcessingRunCreate(
        category=ContractorCategory.HVAC,
        state="CA",
    )
    assert run.category == ContractorCategory.HVAC
    assert run.state == "CA"
    assert run.processed_records == 0


def test_enrichment_attempt_model() -> None:
    """Verify EnrichmentAttemptCreate instantiates properly."""
    attempt = EnrichmentAttemptCreate(
        run_id="run-1",
        company_id="comp-1",
        stage="website_discovery",
        attempt_number=1,
    )
    assert attempt.stage == "website_discovery"
    assert attempt.execution_time_ms == 0.0
