"""Production-grade Christmas Light Installation Lead Enrichment and Classification Pipeline.

Reads contractor leads from CSV, visits company websites, determines genuine installation
service provision vs product sellers, and outputs clean segregated CSV lead files.
100% preserves original input data, columns, multi-line values, and row integrity.
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
from urllib.parse import urlparse

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

from contractor_scraper.classification.christmas_light_classifier import (
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler

logger = get_logger("christmas_light_enricher")
console = Console()

HOLIDAY_PRIORITY_KEYWORDS: list[str] = [
    "/christmas",
    "/holiday",
    "/holiday-lighting",
    "/christmas-lights",
    "/seasonal-lighting",
    "/lighting",
    "/services",
    "/outdoor-lighting",
    "/exterior-lighting",
    "/residential",
    "/commercial",
    "/about",
    "/contact",
    "/quote",
    "/estimate",
]


@dataclass
class ChristmasEnrichmentTelemetry:
    """Execution telemetry and metrics for Christmas Light Installation lead classification."""

    input_file: str
    output_dir: str
    total_records: int = 0
    records_processed: int = 0
    qualified_count: int = 0  # Yes (Production)
    not_qualified_count: int = 0  # No (Excluded)
    uncertain_count: int = 0  # Uncertain
    unreachable_count: int = 0  # Unreachable
    recovered_unreachable_count: int = 0
    duration_seconds: float = 0.0

    def print_summary(self) -> None:
        """Render a clean summary table to the console."""
        table = Table(
            title="Christmas Light Installation Lead Classification Summary",
            header_style="bold white on blue",
            border_style="cyan",
        )
        table.add_column("Classification Category", style="bold white")
        table.add_column("Count", style="bold green", justify="right")
        table.add_column("Share %", style="bold cyan", justify="right")

        total = max(1, self.records_processed)
        table.add_row(
            "[bold green]Qualified Contractors (Production / Yes)[/bold green]",
            f"[bold green]{self.qualified_count:,}[/bold green]",
            f"[bold green]{(self.qualified_count / total * 100):.1f}%[/bold green]",
        )
        table.add_row(
            "[bold yellow]Not Qualified / Excluded (No)[/bold yellow]",
            f"[bold yellow]{self.not_qualified_count:,}[/bold yellow]",
            f"{(self.not_qualified_count / total * 100):.1f}%",
        )
        table.add_row(
            "[bold magenta]Uncertain / Insufficient Evidence[/bold magenta]",
            f"[bold magenta]{self.uncertain_count:,}[/bold magenta]",
            f"{(self.uncertain_count / total * 100):.1f}%",
        )
        table.add_row(
            "[bold red]Unreachable / Connection Errors[/bold red]",
            f"[bold red]{self.unreachable_count:,}[/bold red]",
            f"{(self.unreachable_count / total * 100):.1f}%",
        )
        table.add_section()
        table.add_row("Total Records Examined", f"{self.total_records:,}", "100.0%")
        table.add_row("Total Processed", f"{self.records_processed:,}", "100.0%")
        table.add_row("Execution Duration", f"{self.duration_seconds:.2f}s", "-")
        table.add_row("Output Directory", self.output_dir, "-")

        console.print(table)


class ChristmasLightLeadEnricher:
    """Manages reading, crawling, classifying, and saving Christmas light contractor leads."""

    def __init__(
        self,
        classifier: ChristmasLightClassifier | None = None,
        crawler: WebsiteCrawler | None = None,
        concurrency_limit: int = 12,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.classifier = classifier or ChristmasLightClassifier()
        self.crawler = crawler or WebsiteCrawler(
            timeout_seconds=timeout_seconds,
            max_subpages=6,
            priority_keywords=HOLIDAY_PRIORITY_KEYWORDS,
        )
        self.concurrency_limit = max(1, concurrency_limit)

    def _identify_columns(
        self, headers: list[str]
    ) -> tuple[int, int, int | None, list[str], int | None, int | None]:
        """Identify key column indices while preserving existing header order.

        Returns:
            (clean_web_idx, company_idx, existing_class_idx, final_headers, raw_web_idx, alt_name_idx)
        """
        header_lower = [h.strip().lower() for h in headers]

        # 1. Locate Clean Website URL
        clean_web_idx = None
        for idx, h in enumerate(header_lower):
            if any(
                p in h
                for p in [
                    "clean website url",
                    "clean website",
                    "clean_website",
                    "clean url",
                    "website url",
                ]
            ):
                clean_web_idx = idx
                break

        # 2. Locate Raw Website URL (often contains specific branch/location landing page)
        raw_web_idx = None
        for idx, h in enumerate(header_lower):
            if idx != clean_web_idx and h in ("website", "url", "domain"):
                raw_web_idx = idx
                break

        if clean_web_idx is None:
            clean_web_idx = raw_web_idx if raw_web_idx is not None else 8

        # 3. Locate Company Name
        company_idx = None
        alt_name_idx = None
        for idx, h in enumerate(header_lower):
            if any(
                p in h
                for p in [
                    "company name",
                    "company_name",
                    "business name",
                    "business_name",
                    "contractor_name",
                ]
            ):
                company_idx = idx
                break

        for idx, h in enumerate(header_lower):
            if idx != company_idx and h in ("name", "company"):
                alt_name_idx = idx
                break

        if company_idx is None:
            company_idx = alt_name_idx if alt_name_idx is not None else 0

        # 4. Check for existing classification column
        class_idx = None
        for idx, h in enumerate(header_lower):
            if any(
                p in h
                for p in [
                    "does this company provide christmas light installation",
                    "does provide christmas light installation service",
                    "does provide christmas light installation",
                    "is christmas light installation",
                ]
            ):
                class_idx = idx
                break

        final_headers = list(headers)
        return (
            clean_web_idx,
            company_idx,
            class_idx,
            final_headers,
            raw_web_idx,
            alt_name_idx,
        )

    def _normalize_url(self, raw_url: str) -> str:
        """Clean and normalize URL for HTTP fetching."""
        if not raw_url:
            return ""
        url = raw_url.strip()
        if "%3f" in url.lower() or "%26" in url.lower():
            import urllib.parse

            url = urllib.parse.unquote(url)
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    async def process_leads(
        self,
        input_csv_path: str | Path,
        output_dir: str | Path = "christmas light installation/output",
        limit: int | None = None,
        resume: bool = True,
        retry_unreachable: bool = True,
    ) -> ChristmasEnrichmentTelemetry:
        """Classify contractor leads in input CSV and write 4 categorized output CSV files."""
        start_time = time.time()
        in_path = Path(input_csv_path)
        out_dir = Path(output_dir)

        if not in_path.exists():
            raise FileNotFoundError(f"Input CSV file not found: {in_path}")

        out_dir.mkdir(parents=True, exist_ok=True)

        # Output CSV file paths - exact standard filenames requested by user
        qualified_csv = out_dir / "Qualified Leads.csv"
        not_qualified_csv = out_dir / "Not Qualified Leads.csv"
        uncertain_csv = out_dir / "Uncertain.csv"
        unreachable_csv = out_dir / "Unreachable Leads.csv"
        all_csv = out_dir / "All Leads.csv"

        # Also maintain legacy file names for backwards compatibility with existing test fixtures
        legacy_qualified_csv = out_dir / "christmas_installation_qualified.csv"
        legacy_not_qualified_csv = out_dir / "christmas_installation_not_qualified.csv"
        legacy_uncertain_csv = out_dir / "christmas_installation_uncertain.csv"
        legacy_unreachable_csv = out_dir / "christmas_installation_unreachable.csv"
        legacy_all_csv = out_dir / "christmas_installation_all.csv"

        # Read input CSV preserving all rows and formatting
        with open(in_path, mode="r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise ValueError(f"Input CSV file is empty: {in_path}") from None

            all_raw_rows = list(reader)

        col_info = self._identify_columns(raw_headers)
        web_idx, name_idx, existing_class_idx, base_headers = col_info[:4]
        raw_web_idx = col_info[4] if len(col_info) > 4 else None
        alt_name_idx = col_info[5] if len(col_info) > 5 else None

        # Define output headers: all original headers + explicit classification and diagnostic fields
        target_class_header = "Does this company provide Christmas light installation?"
        extra_headers = [
            target_class_header,
            "Website status",
            "Classification reasoning",
            "Website access error",
            "Evidence",
        ]

        # Determine output header layout
        output_headers = list(base_headers)
        if target_class_header not in output_headers:
            output_headers.append(target_class_header)
        for eh in ["Website status", "Classification reasoning", "Website access error", "Evidence"]:
            if eh not in output_headers:
                output_headers.append(eh)

        classification_col_idx = output_headers.index(target_class_header)
        status_col_idx = output_headers.index("Website status")
        reason_col_idx = output_headers.index("Classification reasoning")
        access_err_col_idx = output_headers.index("Website access error")
        evidence_col_idx = output_headers.index("Evidence")

        total_rows = len(all_raw_rows)
        target_rows = all_raw_rows[:limit] if limit is not None else all_raw_rows
        target_count = len(target_rows)

        # Build output row storage
        evaluated_results: dict[int, ChristmasLightClassificationResult] = {}
        domain_cache: dict[str, ChristmasLightClassificationResult] = {}

        # Resume support: check if All Leads.csv or christmas_installation_all.csv exists
        resume_source = None
        if resume:
            if all_csv.exists() and all_csv.stat().st_size > 0:
                resume_source = all_csv
            elif legacy_all_csv.exists() and legacy_all_csv.stat().st_size > 0:
                resume_source = legacy_all_csv

        if resume_source is not None:
            try:
                with open(resume_source, mode="r", encoding="utf-8", errors="replace", newline="") as r_file:
                    r_reader = csv.reader(r_file)
                    r_hdrs = next(r_reader, [])
                    if target_class_header in r_hdrs:
                        r_c_idx = r_hdrs.index(target_class_header)
                        for idx_r, r_row in enumerate(r_reader):
                            if idx_r < target_count and len(r_row) > r_c_idx:
                                val = r_row[r_c_idx].strip()
                                res_status = (
                                    r_row[r_hdrs.index("Website status")]
                                    if "Website status" in r_hdrs and len(r_row) > r_hdrs.index("Website status")
                                    else "OK"
                                )
                                res_reason = (
                                    r_row[r_hdrs.index("Classification reasoning")]
                                    if "Classification reasoning" in r_hdrs
                                    and len(r_row) > r_hdrs.index("Classification reasoning")
                                    else ""
                                )
                                res_err = (
                                    r_row[r_hdrs.index("Website access error")]
                                    if "Website access error" in r_hdrs
                                    and len(r_row) > r_hdrs.index("Website access error")
                                    else ""
                                )

                                # If retry_unreachable is True, skip loading Unreachable or Uncertain rows so they get re-evaluated
                                if retry_unreachable and (
                                    val in ("Unreachable", "Uncertain")
                                    or res_status in ("Connection Error", "Timeout", "Bot Challenge")
                                ):
                                    continue

                                if val in ("Yes", "No", "Uncertain", "Unreachable"):
                                    dec_lbl = "Yes" if val == "Yes" else ("No" if val == "No" else "Uncertain")
                                    res_obj = ChristmasLightClassificationResult(
                                        classification=val,
                                        decision_label=dec_lbl,
                                        confidence=1.0,
                                        website_status=res_status,
                                        reasoning=res_reason,
                                        access_error=res_err,
                                    )
                                    evaluated_results[idx_r] = res_obj
                                    if len(target_rows[idx_r]) > web_idx:
                                        d_norm = self._normalize_url(target_rows[idx_r][web_idx])
                                        if d_norm:
                                            dom = urlparse(d_norm).netloc.lower().replace("www.", "")
                                            domain_cache[dom] = res_obj
            except Exception as exc:
                logger.debug(f"Could not resume from existing results: {exc}")

        telemetry = ChristmasEnrichmentTelemetry(
            input_file=str(in_path),
            output_dir=str(out_dir),
            total_records=total_rows,
            records_processed=len(evaluated_results),
            qualified_count=sum(1 for r in evaluated_results.values() if r.classification == "Yes"),
            not_qualified_count=sum(1 for r in evaluated_results.values() if r.classification == "No"),
            uncertain_count=sum(1 for r in evaluated_results.values() if r.classification == "Uncertain"),
            unreachable_count=sum(1 for r in evaluated_results.values() if r.classification == "Unreachable"),
        )

        console.print(
            Panel(
                f"[bold white]Christmas Light Installation Lead Classification Engine[/bold white]\n"
                f"[cyan]Input CSV:[/cyan]  {in_path} ([bold yellow]{total_rows:,}[/bold yellow] records)\n"
                f"[cyan]Output Dir:[/cyan] {out_dir}\n"
                f"[cyan]Target:[/cyan]      Processing [bold green]{target_count:,}[/bold green] records "
                f"(concurrency={self.concurrency_limit}, retry_unreachable={retry_unreachable})",
                border_style="blue",
            )
        )

        semaphore = asyncio.Semaphore(self.concurrency_limit)
        lock = asyncio.Lock()

        # Atomic flush function to write all 4 output CSV files + consolidated all_csv
        def flush_all_csvs() -> None:
            qualified_rows: list[list[str]] = []
            not_qualified_rows: list[list[str]] = []
            uncertain_rows: list[list[str]] = []
            unreachable_rows: list[list[str]] = []
            all_rows: list[list[str]] = []

            for r_idx in range(target_count):
                orig_row = list(target_rows[r_idx])
                res = evaluated_results.get(r_idx)

                # Construct complete output row with extra fields
                out_row = list(orig_row)
                while len(out_row) < len(output_headers):
                    out_row.append("")

                if res:
                    out_row[classification_col_idx] = (
                        res.classification
                        if res.classification in ("Yes", "No", "Uncertain")
                        else "Unreachable"
                    )
                    # Also populate existing column in original CSV if it exists
                    if existing_class_idx is not None and existing_class_idx < len(out_row):
                        out_row[existing_class_idx] = res.decision_label

                    out_row[status_col_idx] = res.website_status
                    out_row[reason_col_idx] = res.reasoning
                    out_row[access_err_col_idx] = res.access_error
                    out_row[evidence_col_idx] = "; ".join(res.evidence_phrases)

                    # Group into separate files
                    if res.classification == "Yes":
                        qualified_rows.append(out_row)
                    elif res.classification == "No":
                        not_qualified_rows.append(out_row)
                    elif res.classification == "Uncertain":
                        uncertain_rows.append(out_row)
                    elif res.classification == "Unreachable":
                        unreachable_rows.append(out_row)

                all_rows.append(out_row)

            def write_file(dest: Path, rows: list[list[str]], legacy_dest: Path | None = None) -> None:
                tmp = dest.with_suffix(".tmp")
                with open(tmp, mode="w", encoding="utf-8", errors="replace", newline="") as wf:
                    w = csv.writer(wf, lineterminator="\n")
                    w.writerow(output_headers)
                    w.writerows(rows)
                tmp.replace(dest)
                if legacy_dest is not None:
                    try:
                        import shutil
                        shutil.copyfile(dest, legacy_dest)
                    except Exception as e:
                        logger.debug(f"Could not copy to legacy file {legacy_dest}: {e}")

            write_file(qualified_csv, qualified_rows, legacy_qualified_csv)
            write_file(not_qualified_csv, not_qualified_rows, legacy_not_qualified_csv)
            write_file(uncertain_csv, uncertain_rows, legacy_uncertain_csv)
            write_file(unreachable_csv, unreachable_rows, legacy_unreachable_csv)
            write_file(all_csv, all_rows, legacy_all_csv)

        # Initial flush so files are created immediately
        flush_all_csvs()
        last_flush_time = time.time()

        async def process_record(r_idx: int, progress: Progress, task_id: Any) -> None:
            nonlocal last_flush_time

            # Skip if already loaded from resume
            if r_idx in evaluated_results:
                progress.update(task_id, advance=1)
                return

            row = target_rows[r_idx]

            # Prioritize specific branch/location landing page if available in raw_web_idx
            raw_url = ""
            if raw_web_idx is not None and len(row) > raw_web_idx:
                candidate_raw = row[raw_web_idx].strip()
                if candidate_raw:
                    cand_test = candidate_raw if candidate_raw.startswith(("http://", "https://")) else f"https://{candidate_raw}"
                    p_test = urlparse(cand_test)
                    if p_test.path and p_test.path.strip("/"):
                        raw_url = candidate_raw

            if not raw_url and len(row) > web_idx:
                raw_url = row[web_idx].strip()

            if not raw_url and raw_web_idx is not None and len(row) > raw_web_idx:
                raw_url = row[raw_web_idx].strip()

            # Identify company name
            comp_name = row[name_idx].strip() if len(row) > name_idx else ""
            if not comp_name and alt_name_idx is not None and len(row) > alt_name_idx:
                comp_name = row[alt_name_idx].strip()

            if not raw_url:
                res = ChristmasLightClassificationResult(
                    classification="Uncertain",
                    decision_label="Uncertain",
                    confidence=0.0,
                    website_status="Missing URL",
                    reasoning="Website URL is empty or missing in input record",
                )
                async with lock:
                    evaluated_results[r_idx] = res
                    telemetry.records_processed += 1
                    telemetry.uncertain_count += 1
                    progress.update(task_id, advance=1)
                return

            norm_url = self._normalize_url(raw_url)
            parsed_domain = urlparse(norm_url).netloc.lower().replace("www.", "")

            # Check domain cache to avoid re-crawling identical websites across multiple contacts
            cached_res = None
            async with lock:
                if parsed_domain in domain_cache:
                    cached_res = domain_cache[parsed_domain]

            if cached_res:
                async with lock:
                    evaluated_results[r_idx] = cached_res
                    telemetry.records_processed += 1
                    if cached_res.classification == "Yes":
                        telemetry.qualified_count += 1
                    elif cached_res.classification == "No":
                        telemetry.not_qualified_count += 1
                    elif cached_res.classification == "Uncertain":
                        telemetry.uncertain_count += 1
                    elif cached_res.classification == "Unreachable":
                        telemetry.unreachable_count += 1

                    progress.update(
                        task_id,
                        advance=1,
                        description=(
                            f"[cyan]Cached ({telemetry.records_processed}/{target_count}): "
                            f"{comp_name[:22]} -> {cached_res.classification}[/cyan]"
                        ),
                    )
                return

            # Perform live resilient crawl and evaluation
            async with semaphore:
                progress.update(
                    task_id,
                    description=f"[cyan]Evaluating ({r_idx + 1}/{target_count}): {comp_name[:24]}[/cyan]",
                )
                try:
                    crawl_res: CrawlResult = await self.crawler.crawl_domain(norm_url)
                    class_res: ChristmasLightClassificationResult = self.classifier.evaluate_crawl(
                        crawl_res, company_name=comp_name
                    )
                except Exception as exc:
                    logger.debug(f"Error evaluating {norm_url}: {exc}")
                    class_res = ChristmasLightClassificationResult(
                        classification="Unreachable",
                        decision_label="Uncertain",
                        confidence=0.0,
                        website_status="Connection Error",
                        reasoning=f"Unexpected error visiting {norm_url}: {exc}",
                        access_error=str(exc),
                    )

            async with lock:
                evaluated_results[r_idx] = class_res
                domain_cache[parsed_domain] = class_res
                telemetry.records_processed += 1

                if class_res.classification == "Yes":
                    telemetry.qualified_count += 1
                elif class_res.classification == "No":
                    telemetry.not_qualified_count += 1
                elif class_res.classification == "Uncertain":
                    telemetry.uncertain_count += 1
                elif class_res.classification == "Unreachable":
                    telemetry.unreachable_count += 1

                color = (
                    "green"
                    if class_res.classification == "Yes"
                    else "yellow"
                    if class_res.classification == "No"
                    else "magenta"
                    if class_res.classification == "Uncertain"
                    else "red"
                )
                progress.update(
                    task_id,
                    advance=1,
                    description=(
                        f"[{color}]Done ({telemetry.records_processed}/{target_count})[/{color}]: "
                        f"{comp_name[:20]} -> [{color}]{class_res.classification}[/{color}]"
                    ),
                )

                now = time.time()
                if now - last_flush_time > 8.0 or telemetry.records_processed % 20 == 0:
                    flush_all_csvs()
                    last_flush_time = now

        # Execute with Rich progress bar
        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(bar_width=35),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Classifying Christmas Light Leads...[/cyan]", total=target_count)
            tasks = [process_record(i, progress, task) for i in range(target_count)]
            await asyncio.gather(*tasks)

        # Final atomic flush to ensure everything is saved
        flush_all_csvs()

        telemetry.duration_seconds = time.time() - start_time
        telemetry.print_summary()

        return telemetry
