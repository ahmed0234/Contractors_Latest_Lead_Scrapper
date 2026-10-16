"""Production-grade CSV Enrichment Pipeline for Pest Control Companies.

Reads an existing contractor CSV, discovers and verifies official websites,
classifies whether the business provides genuine on-site pest control services,
and outputs an enriched CSV while 100% preserving all existing data, columns,
duplicate column headers, multi-line values, and row order.
"""

import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import sys
import time
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.company_website_matcher import (
    CompanyWebsiteMatcher,
    CompanyWebsiteMatchResult,
)

logger = get_logger("csv_enricher")
console = Console()


@dataclass
class CSVEnrichmentTelemetry:
    """Execution telemetry and metrics for CSV enrichment."""

    input_file: str
    output_file: str
    total_records: int = 0
    records_processed: int = 0
    websites_discovered: int = 0
    pest_control_yes: int = 0
    pest_control_no: int = 0
    skipped_records: int = 0
    duration_seconds: float = 0.0

    def print_summary(self) -> None:
        """Render a clean summary table to the console."""
        table = Table(
            title="Pest Control Lead Enrichment Summary",
            header_style="bold white on blue",
            border_style="cyan",
        )
        table.add_column("Metric", style="bold white")
        table.add_column("Count / Value", style="bold green", justify="right")

        table.add_row("Input CSV File", self.input_file)
        table.add_row("Output CSV File", self.output_file)
        table.add_row("Total Records Examined", f"{self.total_records:,}")
        table.add_row("Records Processed", f"{self.records_processed:,}")
        table.add_row("Official Websites Discovered", f"{self.websites_discovered:,}")
        web_rate = (
            (self.websites_discovered / self.records_processed * 100)
            if self.records_processed > 0
            else 0.0
        )
        table.add_row("Website Discovery Rate", f"{web_rate:.1f}%")
        table.add_row(
            "Verified Pest Control Companies (Yes)",
            f"[bold green]{self.pest_control_yes:,}[/bold green]",
        )
        table.add_row(
            "Non-Pest / No Verified Website (No)",
            f"[bold yellow]{self.pest_control_no:,}[/bold yellow]",
        )
        yes_rate = (
            (self.pest_control_yes / self.records_processed * 100)
            if self.records_processed > 0
            else 0.0
        )
        table.add_row("Service Qualification Rate", f"{yes_rate:.1f}%")
        table.add_row("Execution Duration", f"{self.duration_seconds:.2f}s")

        console.print(table)


class CSVEnricher:
    """Handles end-to-end reading, matching, classifying, and saving enriched CSV lead files."""

    def __init__(
        self,
        matcher: CompanyWebsiteMatcher | None = None,
        concurrency_limit: int = 5,
    ) -> None:
        self.matcher = matcher or CompanyWebsiteMatcher()
        self.concurrency_limit = max(1, concurrency_limit)

    def _identify_column_indices(
        self, headers: list[str]
    ) -> tuple[dict[str, int | None], list[str], bool, bool]:
        """Locate key field indices while preserving duplicate headers and structure."""
        indices: dict[str, int | None] = {
            "company_name": None,
            "city": None,
            "state": None,
            "phone": None,
            "address": None,
            "zip": None,
            "email": None,
            "website": None,
            "is_pest_control": None,
        }

        # Header patterns
        header_clean = [h.strip().lower() for h in headers]

        # 1. Company Name
        for idx, h in enumerate(header_clean):
            if h in ("company_name", "company", "business_name", "name", "contractor_name"):
                indices["company_name"] = idx
                break

        # 2. City
        for idx, h in enumerate(header_clean):
            if h in ("city", "addr_city", "town"):
                indices["city"] = idx
                break

        # 3. State
        for idx, h in enumerate(header_clean):
            if h in ("state", "addr_state", "st"):
                indices["state"] = idx
                break

        # 4. Phone
        for idx, h in enumerate(header_clean):
            if h in ("phone", "phone_number", "telephone", "phone_1"):
                indices["phone"] = idx
                break

        # 5. Address
        for idx, h in enumerate(header_clean):
            if h in ("address", "addr_line1", "street", "street_address"):
                indices["address"] = idx
                break

        # 6. Zip
        for idx, h in enumerate(header_clean):
            if h in ("zip", "zipcode", "postal_code", "zip_code"):
                indices["zip"] = idx
                break

        # 7. Email (preserve first)
        for idx, h in enumerate(header_clean):
            if h in ("email", "e_mail", "email_address"):
                indices["email"] = idx
                break

        # 8. Website column
        for idx, h in enumerate(header_clean):
            if any(
                p in h
                for p in [
                    "company having website ?",
                    "company having website",
                    "company website",
                    "website",
                    "official_website",
                    "url",
                ]
            ):
                indices["website"] = idx
                break

        # 9. Is pest control company column
        for idx, h in enumerate(header_clean):
            if any(
                p in h
                for p in [
                    "is pest control company ?",
                    "is pest control company",
                    "is_pest_control_company",
                    "is_pest_control",
                    "pest control company ?",
                ]
            ):
                indices["is_pest_control"] = idx
                break

        final_headers = list(headers)
        added_web = False
        added_pest = False

        if indices["website"] is None:
            indices["website"] = len(final_headers)
            final_headers.append("Company Having Website ?")
            added_web = True

        if indices["is_pest_control"] is None:
            indices["is_pest_control"] = len(final_headers)
            final_headers.append("Is Pest Control Company ?")
            added_pest = True

        return indices, final_headers, added_web, added_pest

    async def enrich_csv(
        self,
        input_csv_path: str | Path,
        output_csv_path: str | Path,
        limit: int | None = None,
        resume: bool = True,
        category: str = "pest control",
    ) -> CSVEnrichmentTelemetry:
        """Enrich existing pest control CSV with verified websites and service classifications."""
        start_time = time.time()
        in_path = Path(input_csv_path)
        out_path = Path(output_csv_path)

        if not in_path.exists():
            raise FileNotFoundError(f"Input CSV file not found: {in_path}")

        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Read input CSV safely with utf-8
        with open(in_path, mode="r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise ValueError(f"Input CSV file is empty: {in_path}") from None

            all_raw_rows = list(reader)

        indices, final_headers, added_web, added_pest = self._identify_column_indices(raw_headers)

        if indices["company_name"] is None:
            raise ValueError(
                f"Could not identify company name column in {in_path}. "
                f"Headers present: {raw_headers[:10]}"
            )

        web_idx = indices["website"]
        is_pest_idx = indices["is_pest_control"]
        name_idx = indices["company_name"]
        city_idx = indices["city"]
        state_idx = indices["state"]
        phone_idx = indices["phone"]
        addr_idx = indices["address"]
        zip_idx = indices["zip"]
        email_idx = indices["email"]

        # Ensure all rows have enough columns
        expected_len = len(final_headers)
        rows: list[list[str]] = []
        for r in all_raw_rows:
            row_copy = list(r)
            while len(row_copy) < expected_len:
                row_copy.append("")
            rows.append(row_copy)

        total_rows = len(rows)
        target_rows = rows[:limit] if limit is not None else rows
        target_count = len(target_rows)

        # Check existing output for resume capability
        existing_processed: dict[int, tuple[str, str]] = {}
        if resume and out_path.exists():
            try:
                with open(out_path, mode="r", encoding="utf-8", errors="replace", newline="") as out_f:
                    out_reader = csv.reader(out_f)
                    next(out_reader, None)  # skip header
                    for r_idx, out_row in enumerate(out_reader):
                        if (
                            len(out_row) > max(web_idx, is_pest_idx)
                            and (out_row[web_idx].strip() or out_row[is_pest_idx].strip())
                        ):
                            existing_processed[r_idx] = (out_row[web_idx], out_row[is_pest_idx])
            except Exception as exc:
                logger.debug(f"Could not resume from existing output file: {exc}")

        telemetry = CSVEnrichmentTelemetry(
            input_file=str(in_path),
            output_file=str(out_path),
            total_records=total_rows,
            records_processed=0,
            websites_discovered=0,
            pest_control_yes=0,
            pest_control_no=0,
        )

        console.print(
            Panel(
                f"[bold white]Pest Control Lead CSV Enrichment Engine[/bold white]\n"
                f"[cyan]Input:[/cyan]  {in_path} ([bold yellow]{total_rows:,}[/bold yellow] records)\n"
                f"[cyan]Output:[/cyan] {out_path}\n"
                f"[cyan]Mode:[/cyan]   Processing [bold green]{target_count:,}[/bold green] records "
                f"with concurrency={self.concurrency_limit}",
                border_style="blue",
            )
        )

        semaphore = asyncio.Semaphore(self.concurrency_limit)
        lock = asyncio.Lock()

        # Save checkpoint helper
        def flush_output() -> None:
            temp_out = out_path.with_suffix(".tmp")
            with open(temp_out, mode="w", encoding="utf-8", errors="replace", newline="") as out_file:
                writer = csv.writer(out_file, lineterminator="\n")
                writer.writerow(final_headers)
                writer.writerows(rows)
            temp_out.replace(out_path)

        # Flush initial empty or existing state so output file exists
        flush_output()

        last_flush_time = time.time()

        async def process_record(idx: int, progress: Progress, task_id: Any) -> None:
            nonlocal last_flush_time
            row = rows[idx]
            comp_name = row[name_idx].strip() if name_idx is not None and len(row) > name_idx else ""
            comp_city = row[city_idx].strip() if city_idx is not None and len(row) > city_idx else ""
            comp_state = row[state_idx].strip() if state_idx is not None and len(row) > state_idx else "FL"
            comp_phone = row[phone_idx].strip() if phone_idx is not None and len(row) > phone_idx else ""
            comp_addr = row[addr_idx].strip() if addr_idx is not None and len(row) > addr_idx else ""
            comp_zip = row[zip_idx].strip() if zip_idx is not None and len(row) > zip_idx else ""
            comp_email = row[email_idx].strip() if email_idx is not None and len(row) > email_idx else ""
            existing_web = row[web_idx].strip() if len(row) > web_idx else ""

            # Check if resumed
            if idx in existing_processed:
                e_web, e_pest = existing_processed[idx]
                row[web_idx] = e_web
                row[is_pest_idx] = e_pest
                async with lock:
                    telemetry.records_processed += 1
                    if e_web:
                        telemetry.websites_discovered += 1
                    if e_pest.lower() == "yes":
                        telemetry.pest_control_yes += 1
                    else:
                        telemetry.pest_control_no += 1
                    progress.update(
                        task_id,
                        advance=1,
                        description=f"[cyan]Resumed ({telemetry.records_processed}/{target_count}): {comp_name[:30]}[/cyan]",
                    )
                return

            if not comp_name:
                row[web_idx] = ""
                row[is_pest_idx] = "No"
                async with lock:
                    telemetry.records_processed += 1
                    telemetry.pest_control_no += 1
                    progress.update(task_id, advance=1)
                return

            async with semaphore:
                progress.update(
                    task_id,
                    description=f"[cyan]Matching ({idx + 1}/{target_count}): {comp_name[:28]} ({comp_city})[/cyan]",
                )
                try:
                    result: CompanyWebsiteMatchResult = await self.matcher.match_company_website(
                        company_name=comp_name,
                        city=comp_city,
                        state=comp_state,
                        phone=comp_phone,
                        address=comp_addr,
                        zip_code=comp_zip,
                        email=comp_email,
                        existing_website=existing_web,
                        category=category,
                    )
                except Exception as exc:
                    logger.debug(f"Error matching company {comp_name}: {exc}")
                    result = CompanyWebsiteMatchResult(
                        website="",
                        domain="",
                        is_pest_control=False,
                        decision_label="No",
                        confidence=0.0,
                        verification_status="ERROR",
                        reasoning=str(exc),
                    )

            # Update row in memory
            row[web_idx] = result.website
            row[is_pest_idx] = result.decision_label

            async with lock:
                telemetry.records_processed += 1
                if result.website:
                    telemetry.websites_discovered += 1
                if result.decision_label == "Yes":
                    telemetry.pest_control_yes += 1
                else:
                    telemetry.pest_control_no += 1

                progress.update(
                    task_id,
                    advance=1,
                    description=(
                        f"[green]Done ({telemetry.records_processed}/{target_count})[/green]: "
                        f"{comp_name[:24]} -> "
                        f"[{'green' if result.decision_label == 'Yes' else 'yellow'}]{result.decision_label}[/]"
                    ),
                )

                # Periodic flush to disk every 10 seconds or 20 records
                now = time.time()
                if now - last_flush_time > 10.0 or telemetry.records_processed % 20 == 0:
                    flush_output()
                    last_flush_time = now

        # Execute progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Enriching Pest Control Leads...[/cyan]", total=target_count)
            tasks = [process_record(i, progress, task) for i in range(target_count)]
            await asyncio.gather(*tasks)

        # Final atomic flush to ensure everything is written
        flush_output()

        telemetry.duration_seconds = time.time() - start_time
        telemetry.print_summary()

        return telemetry
