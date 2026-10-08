"""Tests for data normalization routines and deduplication fingerprinting."""

from contractor_scraper.matching.dedup import generate_company_fingerprint
from contractor_scraper.normalization.base import DefaultCompanyNormalizer
from contractor_scraper.normalization.cleaners import (
    clean_city_name,
    clean_company_name,
    clean_state_code,
)
from contractor_scraper.parsing.base import ParsedCompanyRecord


def test_clean_company_name_legal_suffixes() -> None:
    """Test stripping various corporate legal entity suffixes."""
    assert clean_company_name("Summit Roofing Solutions, LLC") == "Summit Roofing Solutions"
    assert clean_company_name("Quality HVAC Services Inc.") == "Quality HVAC Services"
    assert clean_company_name("Precision Plumbing, L.L.C.") == "Precision Plumbing"
    assert clean_company_name("Austin Landscaping Corp.") == "Austin Landscaping"
    assert clean_company_name("Modern Remodeling Company") == "Modern Remodeling"
    assert clean_company_name("Elite Pest Control Ltd.") == "Elite Pest Control"
    assert clean_company_name("  Glow Med Spa ,  LLC  ") == "Glow Med Spa"


def test_clean_company_name_handles_clean_names() -> None:
    """Names without legal suffixes are trimmed and preserved."""
    assert clean_company_name("Apex Roofing") == "Apex Roofing"
    assert clean_company_name("  Super Plumbers  ") == "Super Plumbers"
    assert clean_company_name("") == ""


def test_clean_state_code() -> None:
    """Verify conversion of state names and abbreviations."""
    assert clean_state_code("Texas") == "TX"
    assert clean_state_code("california") == "CA"
    assert clean_state_code("fl") == "FL"
    assert clean_state_code("NY") == "NY"
    assert clean_state_code("InvalidState") is None
    assert clean_state_code("") is None


def test_clean_city_name() -> None:
    """Verify city name standardization."""
    assert clean_city_name("austin") == "Austin"
    assert clean_city_name("SAN FRANCISCO") == "San Francisco"
    assert clean_city_name("  dallas  ") == "Dallas"
    assert clean_city_name("") is None


def test_generate_company_fingerprint_deterministic() -> None:
    """Fingerprint generator must return identical hash regardless of suffix or casing."""
    fp1 = generate_company_fingerprint("Apex Roofing, LLC", "Austin", "Texas")
    fp2 = generate_company_fingerprint("apex roofing Inc.", "austin", "TX")
    fp3 = generate_company_fingerprint("Apex Roofing", "Austin", "tx")
    assert fp1 == fp2 == fp3


def test_generate_company_fingerprint_distinct() -> None:
    """Different companies or cities must yield different fingerprints."""
    fp1 = generate_company_fingerprint("Apex Roofing", "Austin", "TX")
    fp2 = generate_company_fingerprint("Apex Roofing", "Dallas", "TX")
    fp3 = generate_company_fingerprint("Zenith Roofing", "Austin", "TX")
    assert fp1 != fp2
    assert fp1 != fp3


def test_default_company_normalizer() -> None:
    """Verify DefaultCompanyNormalizer transforms ParsedCompanyRecord cleanly."""
    normalizer = DefaultCompanyNormalizer()
    raw = ParsedCompanyRecord(
        source_name="sos_texas",
        source_record_id="SOS-12345",
        raw_name="Blue Sky HVAC, LLC",
        raw_city="fort worth",
        raw_state="Texas",
        raw_license_number="LIC-999",
    )
    result = normalizer.normalize(raw)
    assert result is not None
    assert result.normalized_name == "Blue Sky HVAC"
    assert result.city == "Fort Worth"
    assert result.state == "TX"
    assert result.license_number == "LIC-999"


def test_default_company_normalizer_rejects_missing_city_or_state() -> None:
    """Normalizer returns None if essential location fields cannot be resolved."""
    normalizer = DefaultCompanyNormalizer()
    raw = ParsedCompanyRecord(
        source_name="sos_texas",
        source_record_id="SOS-12345",
        raw_name="Blue Sky HVAC",
        raw_city=None,
        raw_state="Texas",
    )
    assert normalizer.normalize(raw) is None
