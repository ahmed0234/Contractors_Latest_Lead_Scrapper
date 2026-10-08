from contractor_scraper.parsing.sunbiz_parser import SunbizRecordParser
from tests.conftest import make_sunbiz_line


def test_parse_valid_sunbiz_record() -> None:
    """Verify parsing correctly extracts all core fields from 1440-char line."""
    parser = SunbizRecordParser()
    line = make_sunbiz_line()
    record = parser.parse_line(line)

    assert record is not None
    assert record.source_record_id == "P25000001234"
    assert record.raw_name == "APEX ROOFING CONTRACTORS LLC"
    assert record.raw_city == "ORLANDO"
    assert record.raw_state == "FL"
    assert record.raw_filing_date == "01152025"
    assert record.raw_metadata["filing_year"] == 2025
    assert record.raw_metadata["is_active"] is True


def test_parse_inactive_record() -> None:
    """Verify inactive records flag is_active as False."""
    parser = SunbizRecordParser()
    line = make_sunbiz_line(status="I")
    record = parser.parse_line(line)

    assert record is not None
    assert record.raw_metadata["is_active"] is False


def test_parse_short_or_corrupt_line() -> None:
    """Verify parser returns None for corrupt short lines."""
    parser = SunbizRecordParser()
    assert parser.parse_line("") is None
    assert parser.parse_line("TOO SHORT") is None


def test_stream_records_year_filter() -> None:
    """Verify stream_records filters by requested filing year."""
    parser = SunbizRecordParser()
    lines = [
        make_sunbiz_line(name="COMPANY 2025", file_date="01012025"),
        make_sunbiz_line(name="COMPANY 2024", file_date="05052024"),
        make_sunbiz_line(name="COMPANY 2025 B", file_date="07072025"),
    ]

    records_2025 = list(parser.stream_records(lines, target_year=2025))
    assert len(records_2025) == 2
    assert records_2025[0].raw_name == "COMPANY 2025"
    assert records_2025[1].raw_name == "COMPANY 2025 B"

    records_2024 = list(parser.stream_records(lines, target_year=2024))
    assert len(records_2024) == 1
    assert records_2024[0].raw_name == "COMPANY 2024"
