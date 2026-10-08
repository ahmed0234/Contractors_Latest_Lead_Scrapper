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
        """Export leads to CSV with exactly the five mandatory columns."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            df = self.to_polars_dataframe(leads)
            df.write_csv(output_path)
            return output_path
        except Exception as exc:
            raise StorageError(f"Failed to export leads to CSV '{output_path}': {exc}") from exc

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
