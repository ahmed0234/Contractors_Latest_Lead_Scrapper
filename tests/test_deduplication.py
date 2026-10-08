"""Tests for multi-signal lead deduplication."""

from contractor_scraper.matching.dedup import LeadDeduplicator
from contractor_scraper.models.lead import FinalLead


def test_deduplicator_by_fingerprint() -> None:
    """Verify deduplication detects identical companies despite legal suffix variation."""
    dedup = LeadDeduplicator()

    lead1 = FinalLead(
        company_name="Apex Roofing LLC",
        email="info@apexroofing.com",
        website="https://apexroofing.com",
        city="Orlando",
        state="FL",
    )
    lead2 = FinalLead(
        company_name="Apex Roofing Inc.",
        email="sales@otherdomain.com",
        website="https://otherdomain.com",
        city="Orlando",
        state="FL",
    )

    assert dedup.is_duplicate(lead1) is False
    dedup.register(lead1)

    # lead2 has identical canonical fingerprint (apexroofing:orlando:fl)
    assert dedup.is_duplicate(lead2) is True


def test_deduplicator_by_domain() -> None:
    """Verify deduplication detects identical website domains."""
    dedup = LeadDeduplicator()

    lead1 = FinalLead(
        company_name="Apex Roofing",
        email="info@apexroofing.com",
        website="https://apexroofing.com",
        city="Orlando",
        state="FL",
    )
    lead2 = FinalLead(
        company_name="Summit Roofing",
        email="summit@apexroofing.com",
        website="https://www.apexroofing.com/contact",
        city="Tampa",
        state="FL",
    )

    dedup.register(lead1)
    assert dedup.is_duplicate(lead2) is True


def test_deduplicator_by_email() -> None:
    """Verify deduplication detects identical email addresses."""
    dedup = LeadDeduplicator()

    lead1 = FinalLead(
        company_name="Apex Roofing",
        email="shared@roofpros.com",
        website="https://apexroofing.com",
        city="Orlando",
        state="FL",
    )
    lead2 = FinalLead(
        company_name="Distinct Roofing",
        email="shared@roofpros.com",
        website="https://distinctroofs.com",
        city="Miami",
        state="FL",
    )

    dedup.register(lead1)
    assert dedup.is_duplicate(lead2) is True


def test_deduplicate_list() -> None:
    """Verify deduplicate_list preserves unique leads and eliminates duplicates."""
    dedup = LeadDeduplicator()

    leads = [
        FinalLead(
            company_name="Apex Roofing",
            email="info@apexroofing.com",
            website="https://apexroofing.com",
            city="Orlando",
            state="FL",
        ),
        FinalLead(
            company_name="Apex Roofing LLC",  # Duplicate by fingerprint
            email="sales@apexroofing.com",
            website="https://apexroofing.com",
            city="Orlando",
            state="FL",
        ),
        FinalLead(
            company_name="Coastal Roofing",
            email="info@coastalroofing.com",
            website="https://coastalroofing.com",
            city="Tampa",
            state="FL",
        ),
    ]

    unique = dedup.deduplicate_list(leads)
    assert len(unique) == 2
    assert unique[0].company_name == "Apex Roofing"
    assert unique[1].company_name == "Coastal Roofing"
