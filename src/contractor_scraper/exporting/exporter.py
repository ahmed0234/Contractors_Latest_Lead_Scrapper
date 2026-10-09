"""Lead exporting subsystem supporting CSV, Parquet, and JSON formats using Polars."""

import json
from pathlib import Path

import polars as pl

from contractor_scraper.core.exceptions import StorageError
from contractor_scraper.models.lead import FinalLead


class LeadExporter:
    """Exports verified high-quality leads strictly containing the mandatory five fields."""

    @staticmethod
    def validate_leads(leads: list[FinalLead]) -> tuple[list[FinalLead], list[dict[str, str]]]:
        """Validates a list of leads, ensuring mandatory business identity is present."""
        valid_leads: list[FinalLead] = []
        excluded: list[dict[str, str]] = []

        for lead in leads:
            if not lead.company_name or not lead.city or not lead.state:
                excluded.append(
                    {
                        "company_name": lead.company_name,
                        "reason": "Missing core business identity (company name or location)",
                    }
                )
            else:
                valid_leads.append(lead)

        return valid_leads, excluded

    @staticmethod
    def to_polars_dataframe(leads: list[FinalLead]) -> pl.DataFrame:
        """Converts verified leads into a standardized Polars DataFrame."""
        if not leads:
            return pl.DataFrame(
                schema={
                    "Company Name": pl.Utf8,
                    "Website": pl.Utf8,
                    "Email": pl.Utf8,
                    "City": pl.Utf8,
                    "State": pl.Utf8,
                    "Formation Date": pl.Utf8,
                    "Document Number": pl.Utf8,
                    "Company Status": pl.Utf8,
                    "Street Address": pl.Utf8,
                    "ZIP Code": pl.Utf8,
                    "Industry": pl.Utf8,
                }
            )

        data = [lead.to_output_dict() for lead in leads]
        return pl.DataFrame(data)

    def export_to_csv(self, leads: list[FinalLead], output_path: Path) -> Path:
        """Export leads to CSV atomically using temporary file to prevent corruption on interruption."""
        import os

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f".tmp_{output_path.name}")

        try:
            df = self.to_polars_dataframe(leads)
            df.write_csv(temp_path)
            # Atomic filesystem replacement
            os.replace(temp_path, output_path)
            return output_path
        except Exception as exc:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise StorageError(f"Failed to export leads to CSV '{output_path}': {exc}") from exc

    def export_to_csv_atomic(self, leads: list[FinalLead], output_path: Path) -> Path:
        """Explicit alias for atomic CSV export."""
        return self.export_to_csv(leads, output_path)

    @staticmethod
    def load_existing_leads_from_csv(csv_path: Path) -> list[FinalLead]:
        """Load previously saved leads from disk to support seamless resumes."""
        if not csv_path.exists() or csv_path.stat().st_size == 0:
            return []

        try:
            df = pl.read_csv(csv_path, infer_schema_length=1000)
            if df.is_empty():
                return []

            leads: list[FinalLead] = []
            for row in df.iter_rows(named=True):
                company_name = str(row.get("Company Name") or "").strip()
                city = str(row.get("City") or "").strip()
                state = str(row.get("State") or "FL").strip()
                if not company_name or not city:
                    continue

                leads.append(
                    FinalLead(
                        company_name=company_name,
                        website=str(row.get("Website") or "").strip(),
                        email=str(row.get("Email") or "").strip(),
                        city=city,
                        state=state,
                        formation_date=str(row.get("Formation Date") or "").strip(),
                        document_number=str(row.get("Document Number") or "").strip(),
                        company_status=str(row.get("Company Status") or "Active").strip(),
                        street_address=str(row.get("Street Address") or "").strip(),
                        zip_code=str(row.get("ZIP Code") or "").strip(),
                        industry=str(row.get("Industry") or "").strip(),
                    )
                )
            return leads
        except Exception:
            return []

    def export_to_parquet(self, leads: list[FinalLead], output_path: Path) -> Path:
        """Export leads to high-compression Parquet format using Polars."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            df = self.to_polars_dataframe(leads)
            df.write_parquet(output_path, compression="zstd")
            return output_path
        except Exception as exc:
            raise StorageError(f"Failed to export leads to Parquet '{output_path}': {exc}") from exc

    def export_to_json(self, leads: list[FinalLead], output_path: Path) -> Path:
        """Export leads to structured JSON format."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            data = [lead.to_output_dict() for lead in leads]
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return output_path
        except Exception as exc:
            raise StorageError(f"Failed to export leads to JSON '{output_path}': {exc}") from exc
