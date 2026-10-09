"""High-speed streaming parser for Florida Sunbiz fixed-width corporate data records."""

from collections.abc import Iterable, Iterator

from contractor_scraper.parsing.base import BaseParser, ParsedCompanyRecord


class SunbizRecordParser(BaseParser):
    """Parser for Florida Division of Corporations fixed-length 1440-byte text records.

    Field mapping reference (Florida Department of State):
    - Field 1  (0:12)   : Corporation Number
    - Field 2  (12:204) : Corporation Name
    - Field 3  (204:205): Status ('A' Active, 'I' Inactive)
    - Field 4  (205:220): Filing Type (DOMP, FLAL, etc.)
    - Field 5  (220:262): Address 1 (Principal Address)
    - Field 6  (262:304): Address 2
    - Field 7  (304:332): City
    - Field 8  (332:334): State (defaults to 'FL' if blank)
    - Field 9  (334:344): Zip Code
    - Field 17 (472:480): File Date (MMDDYYYY)
    - Field 18 (480:494): FEI Number
    - Field 31 (544:586): Registered Agent Name
    """

    def parse_line(self, line: str) -> ParsedCompanyRecord | None:
        """Parse a single fixed-width Sunbiz line into a ParsedCompanyRecord."""
        if len(line) < 332:
            return None

        corp_number = line[0:12].strip()
        corp_name = line[12:204].strip()
        if not corp_name:
            return None

        status = line[204:205].strip().upper() if len(line) >= 205 else "A"
        filing_type = line[205:220].strip() if len(line) >= 220 else ""
        addr1 = line[220:262].strip() if len(line) >= 262 else ""
        addr2 = line[262:304].strip() if len(line) >= 304 else ""
        city = line[304:332].strip() if len(line) >= 332 else ""
        state = line[332:334].strip().upper() if len(line) >= 334 else ""
        if not state:
            state = "FL"
        zip_code = line[334:344].strip() if len(line) >= 344 else ""

        file_date_raw = line[472:480].strip() if len(line) >= 480 else ""
        filing_year: int | None = None
        if len(file_date_raw) == 8 and file_date_raw.isdigit():
            # Format is MMDDYYYY -> year is last 4 digits
            try:
                filing_year = int(file_date_raw[4:])
            except ValueError:
                filing_year = None

        full_address = f"{addr1} {addr2}".strip() if addr2 else addr1

        metadata = {
            "status": status,
            "filing_type": filing_type,
            "address1": addr1,
            "address2": addr2,
            "street_address": full_address,
            "zip": zip_code,
            "zip_code": zip_code,
            "file_date": file_date_raw,
            "filing_year": filing_year,
            "is_active": status == "A",
        }

        return ParsedCompanyRecord(
            source_name="florida_sunbiz",
            source_record_id=corp_number,
            raw_name=corp_name,
            raw_city=city,
            raw_state=state,
            raw_address=full_address,
            raw_filing_date=file_date_raw,
            raw_metadata=metadata,
        )

    def parse_payload(
        self, raw_content: str, metadata: dict | None = None
    ) -> list[ParsedCompanyRecord]:
        """Parse raw multiline text into records."""
        records = []
        for line in raw_content.splitlines():
            rec = self.parse_line(line)
            if rec:
                records.append(rec)
        return records

    def stream_records(
        self,
        lines_iterator: Iterable[str],
        target_year: int | None = None,
        active_only: bool = True,
    ) -> Iterator[ParsedCompanyRecord]:
        """Stream parsed records with optional year and active status filtering."""
        for line in lines_iterator:
            rec = self.parse_line(line)
            if not rec:
                continue

            if active_only and not rec.raw_metadata.get("is_active", True):
                continue

            if target_year is not None:
                record_year = rec.raw_metadata.get("filing_year")
                if record_year is not None and record_year != target_year:
                    continue

            yield rec
