"""Shared orchestration for website-based CSV lead classification pipelines.

A niche pipeline (Christmas lighting, auto repair, ...) reads a lead CSV, crawls each unique website
once, classifies the leads from the crawl, and writes segregated output CSVs. Everything except the
classification itself lives here:

- Original columns, multi-line values, and row order are preserved verbatim; exact duplicate rows are
  dropped; enrichment columns are appended (or reused when the input already carries them).
- Optional duplicate groups (`groups_duplicates` + `_duplicate_key`, e.g. same name AND same email):
  only the first row of a group is classified, its verdict is applied to every row of the group, all
  rows are kept in the outputs, and every row of every group is also written to duplicates.csv.
- Optional per-row reason report (`reason_report_filename`) for reviewing borderline verdicts.
- Rows sharing a website are crawled once. A niche either shares one verdict across those rows
  (`shares_result_per_site`) or classifies each row separately against the shared crawl (needed when
  one domain hosts many unrelated businesses, e.g. an accountant's or parent company's email domain).
- Bounded global concurrency; per-domain politeness lives in the crawler.
- Incremental JSONL checkpoint keyed by the input file's SHA-256 and the classifier version, so an
  interrupted run resumes without re-crawling and stale verdicts are never reused.
- A patient second pass re-crawls temporary failures; output CSVs are flushed atomically during the
  run; post-run QA checks every lead lands in exactly one output file.
"""

import asyncio
import csv
import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar
from urllib.parse import unquote, urlparse

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
from contractor_scraper.discovery.crawler import (
    SOCIAL_PROFILE_DOMAINS,
    TRANSIENT_STATUS_CATEGORIES,
    WebsiteCrawler,
)

logger = get_logger("niche_enricher")
console = Console()

# Live-site failures for which a public archive snapshot is consulted (the site exists but could not be read)
ARCHIVE_FALLBACK_CATEGORIES: frozenset[str] = TRANSIENT_STATUS_CATEGORIES | frozenset(
    {"Bot Challenge", "HTTP Error 401", "HTTP Error 403", "HTTP Error 406", "HTTP Error 451", "Redirect Loop"}
)
MAX_ARCHIVE_AGE_YEARS = 4

# Output files (one per classification bucket) plus the consolidated sheet
OUTPUT_FILENAMES: dict[str, str] = {
    "Yes": "qualified_leads.csv",
    "No": "not_qualified_leads.csv",
    "Uncertain": "uncertain_leads.csv",
    "Unreachable": "unreachable_leads.csv",
}
ALL_LEADS_FILENAME = "all_leads.csv"
# Extra column written to all_leads.csv only (the per-category sheets keep their structure)
NICHE_COLUMN = "is our niche company ?"
NICHE_VALUES: dict[str, str] = {
    "Yes": "yes",
    "No": "no",
    "Uncertain": "uncertain",
    "Unreachable": "unreachable",
}
DUPLICATES_FILENAME = "duplicates.csv"
# Extra columns of duplicates.csv (after the all_leads.csv layout)
# Duplicate_Classified_Row: input spreadsheet row whose verdict the whole group shares
DUPLICATE_HEADERS = ["Duplicate_Group", "Duplicate_Count", "Duplicate_Classified_Row"]
CHECKPOINT_FILENAME = "crawl_checkpoint.jsonl"
REPORT_FILENAME = "run_report.json"

# Hosted-platform roots where only the full profile path identifies the business
PROFILE_PLATFORM_HOSTS: frozenset[str] = SOCIAL_PROFILE_DOMAINS | frozenset({"sites.google.com"})


class NicheResult(Protocol):
    """Minimal interface every niche classification result provides."""

    classification: str  # output bucket: Yes | No | Uncertain | Unreachable
    confidence: float
    website_status: str
    reasoning: str

    def to_dict(self) -> dict[str, Any]: ...


R = TypeVar("R", bound=NicheResult)


@dataclass
class NicheEnrichmentTelemetry:
    """Execution telemetry and metrics for a niche lead classification run."""

    input_file: str
    output_dir: str
    total_records: int = 0
    duplicate_rows_removed: int = 0
    duplicate_groups: int = 0  # groups of rows sharing the niche's duplicate key (classified once each)
    duplicate_rows: int = 0  # rows in those groups, first occurrences included
    qualified_duplicates_removed: int = 0  # duplicate rows left out of qualified_leads.csv
    groups_duplicates: bool = False
    unique_websites: int = 0
    records_processed: int = 0
    qualified_count: int = 0  # Yes (Qualified)
    not_qualified_count: int = 0  # No (Excluded)
    uncertain_count: int = 0  # Uncertain
    unreachable_count: int = 0  # Unreachable
    websites_evaluated: int = 0  # websites whose content was successfully evaluated
    websites_from_checkpoint: int = 0
    archived_evaluations: int = 0  # verdicts based on a Wayback Machine snapshot
    retried_websites: int = 0
    recovered_unreachable_count: int = 0
    duration_seconds: float = 0.0
    output_files: dict[str, str] = field(default_factory=dict)
    persistent_failures: list[dict[str, str]] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    summary_title: str = "Lead Classification Summary"

    @property
    def qualification_rate(self) -> float:
        return (self.qualified_count / self.records_processed * 100.0) if self.records_processed else 0.0

    def print_summary(self) -> None:
        """Render a clean summary table to the console."""
        table = Table(title=self.summary_title, header_style="bold white on blue", border_style="cyan")
        table.add_column("Classification Category", style="bold white")
        table.add_column("Count", style="bold green", justify="right")
        table.add_column("Share %", style="bold cyan", justify="right")

        total = max(1, self.records_processed)
        table.add_row(
            "[bold green]Qualified[/bold green]",
            f"[bold green]{self.qualified_count:,}[/bold green]",
            f"[bold green]{(self.qualified_count / total * 100):.1f}%[/bold green]",
        )
        table.add_row(
            "[bold yellow]Rejected (not_qualified_leads.csv)[/bold yellow]",
            f"[bold yellow]{self.not_qualified_count:,}[/bold yellow]",
            f"{(self.not_qualified_count / total * 100):.1f}%",
        )
        table.add_row(
            "[bold magenta]Needs Review (uncertain_leads.csv)[/bold magenta]",
            f"[bold magenta]{self.uncertain_count:,}[/bold magenta]",
            f"{(self.uncertain_count / total * 100):.1f}%",
        )
        table.add_row(
            "[bold red]Unverified (unreachable_leads.csv)[/bold red]",
            f"[bold red]{self.unreachable_count:,}[/bold red]",
            f"{(self.unreachable_count / total * 100):.1f}%",
        )
        table.add_section()
        table.add_row("Input rows", f"{self.total_records:,}", "-")
        table.add_row("Exact duplicate rows removed", f"{self.duplicate_rows_removed:,}", "-")
        if self.groups_duplicates:
            table.add_row("Duplicate groups (classified once)", f"{self.duplicate_groups:,}", "-")
            table.add_row("Rows in duplicate groups (duplicates.csv)", f"{self.duplicate_rows:,}", "-")
            table.add_row("Duplicates left out of qualified_leads.csv", f"{self.qualified_duplicates_removed:,}", "-")
        table.add_row("Leads classified", f"{self.records_processed:,}", "100.0%")
        table.add_row("Unique websites", f"{self.unique_websites:,}", "-")
        table.add_row("Websites successfully evaluated", f"{self.websites_evaluated:,}", "-")
        table.add_row("Evaluated via archived snapshot", f"{self.archived_evaluations:,}", "-")
        table.add_row("Websites retried (patient pass)", f"{self.retried_websites:,}", "-")
        table.add_row("Recovered on retry", f"{self.recovered_unreachable_count:,}", "-")
        table.add_row("QA validation", "PASSED" if self.validation.get("passed") else "FAILED", "-")
        table.add_row("Execution Duration", f"{self.duration_seconds:.1f}s", "-")
        table.add_row("Output Directory", self.output_dir, "-")

        console.print(table)


@dataclass
class EnrichmentLead:
    """One input row plus the values the pipeline needs from it."""

    index: int  # position among the input data rows (stable for a given input file)
    row: list[str]
    company: str
    crawl_url: str
    crawl_key: str
    fields: dict[str, str] = field(default_factory=dict)  # niche-specific row values (dba, city, phone, ...)


@dataclass
class ColumnMap:
    """Input column positions detected from the header row."""

    website: int
    company: int
    existing_class: int | None = None  # pre-existing verdict column filled with the decision label
    raw_website: int | None = None
    alt_name: int | None = None
    extra: dict[str, int | None] = field(default_factory=dict)


def find_column(headers: list[str], *patterns: str, exact: bool = False, exclude: set[int] | None = None) -> int | None:
    """Index of the first header containing (or equal to, with `exact`) one of `patterns` (case-insensitive)."""
    lowered = [h.strip().lower() for h in headers]
    for pattern in patterns:
        p = pattern.lower()
        for idx, h in enumerate(lowered):
            if exclude and idx in exclude:
                continue
            if (h == p) if exact else (p in h):
                return idx
    return None


class NicheLeadEnricher(ABC, Generic[R]):
    """Reads, crawls, classifies, checkpoints, and writes niche leads; subclasses supply the classification."""

    pipeline_title: str = "Lead Classification Engine"
    summary_title: str = "Lead Classification Summary"
    classifier_version: str = ""
    enrichment_headers: list[str] = []
    # True: rows sharing a website share one verdict. False: each row is classified against the shared crawl.
    shares_result_per_site: bool = True
    # True: rows sharing `_duplicate_key` are classified once (verdict applied to all) and listed in
    # duplicates.csv; exact duplicate rows are then kept (they are part of a duplicate group) instead of dropped
    groups_duplicates: bool = False
    # True (with `groups_duplicates`): qualified_leads.csv lists each duplicate group once (its first
    # occurrence); every other sheet keeps all rows, and duplicates.csv still lists every group member
    dedupe_qualified_sheet: bool = False
    # File name of a per-row reason report (None = not written); see `_reason_report_row`
    reason_report_filename: str | None = None
    telemetry_cls: type[NicheEnrichmentTelemetry] = NicheEnrichmentTelemetry

    def __init__(
        self,
        crawler: WebsiteCrawler,
        concurrency_limit: int = 8,
        archive_fallback: bool = True,
    ) -> None:
        self.crawler = crawler
        self.concurrency_limit = max(1, concurrency_limit)
        self.archive_fallback = archive_fallback

    # ------------------------------------------------------------------ subclass hooks
    @abstractmethod
    def _column_map(self, headers: list[str]) -> ColumnMap:
        """Detect the input columns this niche needs."""

    def _lead_fields(self, row: list[str], columns: ColumnMap) -> dict[str, str]:
        """Niche-specific values carried on each lead (default: none)."""
        return {}

    @abstractmethod
    def _result_from_dict(self, data: dict[str, Any]) -> R:
        """Rebuild a result from its checkpoint form."""

    @abstractmethod
    def _missing_url_result(self, lead: EnrichmentLead) -> R:
        """Verdict for a lead without a usable website URL."""

    @abstractmethod
    async def _evaluate_group(self, crawler: WebsiteCrawler, leads: list[EnrichmentLead]) -> dict[int, R]:
        """Crawl the group's shared website and return a verdict per lead index."""

    @abstractmethod
    def _needs_retry(self, res: R) -> bool:
        """Whether a verdict came from a temporary failure worth a patient re-crawl."""

    @abstractmethod
    def _enrichment_values(self, res: R | None) -> list[str]:
        """Values for `enrichment_headers` (None = not processed yet)."""

    def _decision_value(self, res: R) -> str:
        """Value written to a pre-existing verdict column in the input."""
        return getattr(res, "decision_label", res.classification)

    def _status_label(self, res: R) -> str:
        return str(getattr(res, "qualification_status", res.classification))

    def _input_column_values(self, res: R, columns: ColumnMap) -> dict[int, str]:
        """Values written into other pre-existing input columns, by column index (default: none).

        An empty value leaves the original cell untouched.
        """
        return {}

    def _has_evidence(self, res: R) -> bool:
        """QA: a qualified verdict must carry evidence."""
        return bool(res.reasoning)

    def _site_evaluated(self, res: R) -> bool:
        return res.classification in ("Yes", "No", "Uncertain") and res.website_status != "Missing URL"

    def _site_unreachable(self, res: R) -> bool:
        return res.classification == "Unreachable"

    def _access_error(self, res: R) -> str:
        return str(getattr(res, "access_error", "") or res.reasoning)

    def _extra_validation(self, leads: list[EnrichmentLead], lead_result: Callable[[EnrichmentLead], R | None]) -> dict[str, Any]:
        return {}

    def _duplicate_key(self, lead: EnrichmentLead) -> str | None:
        """Key under which rows count as duplicates (None = never a duplicate). Used with `groups_duplicates`."""
        return None

    def _duplicate_representative(self, group: list[EnrichmentLead]) -> EnrichmentLead:
        """The row of a duplicate group that is classified (default: the first occurrence)."""
        return group[0]

    def _reason_report_headers(self) -> list[str]:
        return ["Input_Row", "Company", "Website", "Classification", "Confidence", "Reason", "Duplicate_Group"]

    def _reason_report_row(self, lead: EnrichmentLead, res: R | None, duplicate_group: str) -> list[str]:
        """One row of the reason report (`duplicate_group` is "" for rows outside a duplicate group)."""
        if res is None:
            return [str(lead.index + 2), lead.company, lead.crawl_url, "Pending", "", "", duplicate_group]
        return [
            str(lead.index + 2), lead.company, lead.crawl_url, res.classification,
            f"{res.confidence:.2f}", res.reasoning, duplicate_group,
        ]

    def _default_output_dir(self, in_path: Path) -> Path:
        # <folder>/input/<file>.csv -> <folder>/output/<stem>; otherwise <input folder>/output/<stem>
        base = in_path.parent.parent if in_path.parent.name.lower() == "input" else in_path.parent
        return base / "output" / in_path.stem

    # ------------------------------------------------------------------ shared helpers
    def _normalize_url(self, raw_url: str) -> str:
        """Clean and normalize URL for HTTP fetching (drops query strings and tracking parameters)."""
        if not raw_url:
            return ""
        url = raw_url.strip()
        if "%3f" in url.lower() or "%26" in url.lower():
            url = unquote(url)
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        parsed = urlparse(url)
        if not parsed.netloc or "." not in parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _row_crawl_url(self, row: list[str], columns: ColumnMap) -> str:
        """URL to crawl for an input row (default: the website column, see `_select_crawl_url`)."""

        def cell(idx: int | None) -> str:
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        return self._select_crawl_url(cell(columns.website), cell(columns.raw_website))

    def _select_crawl_url(self, clean_raw: str, raw_raw: str) -> str:
        """Choose the URL to crawl: the clean website, or a same-domain branch/profile page when more specific."""
        clean = self._normalize_url(clean_raw)
        raw = self._normalize_url(raw_raw)
        if not clean:
            return raw
        if not raw:
            return clean
        c_dom = urlparse(clean).netloc.lower().replace("www.", "")
        r_dom = urlparse(raw).netloc.lower().replace("www.", "")
        if c_dom == r_dom and urlparse(raw).path.strip("/"):
            # Branch/location landing page (crawler also visits the domain root) or a profile path on a platform
            return raw
        return clean

    @staticmethod
    def _crawl_key(url: str) -> str:
        parsed = urlparse(url)
        domain = parsed.netloc.lower().replace("www.", "")
        if domain in PROFILE_PLATFORM_HOSTS and parsed.path.strip("/"):
            return f"{domain}{parsed.path.rstrip('/').lower()}"
        return domain

    @staticmethod
    def _file_fingerprint(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _make_patient_crawler(self) -> WebsiteCrawler:
        """A slower, more forgiving crawler for the second pass over temporary failures."""
        if type(self.crawler) is not WebsiteCrawler:
            return self.crawler  # custom/mock crawler: reuse as-is
        cfg = self.crawler.config
        patient = replace(
            cfg,
            connect_timeout=cfg.connect_timeout * 1.75,
            read_timeout=cfg.read_timeout * 1.75,
            domain_timeout=(cfg.domain_timeout or 60.0) * 1.5,
            max_retries=cfg.max_retries + 1,
            per_domain_concurrency=max(1, min(2, cfg.per_domain_concurrency)),
            request_delay=max(cfg.request_delay, 0.75),
            backoff_base=cfg.backoff_base * 1.5,
        )
        patient_crawler = WebsiteCrawler(
            cache_dir=self.crawler.cache_dir,
            timeout_seconds=self.crawler.timeout_seconds * 1.75,
            max_subpages=self.crawler.max_subpages,
            priority_keywords=self.crawler.priority_keywords,
            custom_user_agent=self.crawler.custom_user_agent,
            config=patient,
        )
        patient_crawler.archive_throttle = self.crawler.archive_throttle
        return patient_crawler

    # ------------------------------------------------------------------ main pipeline
    async def process_leads(
        self,
        input_csv_path: str | Path,
        output_dir: str | Path | None = None,
        limit: int | None = None,
        resume: bool = True,
        retry_unreachable: bool = True,
        second_pass: bool = True,
    ) -> NicheEnrichmentTelemetry:
        """Classify the leads in the input CSV and write the categorized output CSV files."""
        start_time = time.time()
        # Per-request HTTP logs would drown the progress display
        logging.getLogger("httpx").setLevel(logging.WARNING)
        in_path = Path(input_csv_path)
        if not in_path.exists():
            raise FileNotFoundError(f"Input CSV file not found: {in_path}")
        out_dir = Path(output_dir) if output_dir else self._default_output_dir(in_path)
        out_dir.mkdir(parents=True, exist_ok=True)

        output_paths = {cls: out_dir / name for cls, name in OUTPUT_FILENAMES.items()}
        all_csv = out_dir / ALL_LEADS_FILENAME
        duplicates_csv = out_dir / DUPLICATES_FILENAME
        reason_csv = out_dir / self.reason_report_filename if self.reason_report_filename else None
        checkpoint_path = out_dir / CHECKPOINT_FILENAME
        report_path = out_dir / REPORT_FILENAME

        # Read input CSV preserving all rows and formatting
        with open(in_path, encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_headers = next(reader)
            except StopIteration:
                raise ValueError(f"Input CSV file is empty: {in_path}") from None
            all_raw_rows = [r for r in reader if any(cell.strip() for cell in r)]

        # A previous all_leads.csv fed back in carries our niche column; drop it so it is recomputed
        # and never leaks into the per-category sheets
        stripped = [h.strip() for h in raw_headers]
        if NICHE_COLUMN in stripped:
            drop = stripped.index(NICHE_COLUMN)
            raw_headers = raw_headers[:drop] + raw_headers[drop + 1 :]
            all_raw_rows = [r[:drop] + r[drop + 1 :] for r in all_raw_rows]

        columns = self._column_map(raw_headers)

        # Output header layout: original headers + enrichment columns (reused if already present)
        output_headers = list(raw_headers)
        for eh in self.enrichment_headers:
            if eh not in output_headers:
                output_headers.append(eh)
        enrichment_idx = [output_headers.index(eh) for eh in self.enrichment_headers]
        # all_leads.csv layout: the shared layout plus the niche column
        all_headers = [*output_headers, NICHE_COLUMN]
        niche_idx = len(output_headers)

        target_rows = all_raw_rows[:limit] if limit is not None else all_raw_rows

        # Build lead list, dropping exact duplicate rows (kept when duplicate groups handle them)
        leads: list[EnrichmentLead] = []
        seen_rows: set[tuple[str, ...]] = set()
        duplicates_removed = 0
        for i, row in enumerate(target_rows):
            row_key = tuple(c.strip() for c in row)
            if row_key in seen_rows and not self.groups_duplicates:
                duplicates_removed += 1
                continue
            seen_rows.add(row_key)

            def cell(idx: int | None, r: list[str] = row) -> str:
                return r[idx].strip() if idx is not None and idx < len(r) else ""

            company = cell(columns.company) or cell(columns.alt_name)
            crawl_url = self._row_crawl_url(row, columns)
            leads.append(
                EnrichmentLead(
                    index=i,
                    row=list(row),
                    company=company,
                    crawl_url=crawl_url,
                    crawl_key=self._crawl_key(crawl_url) if crawl_url else "",
                    fields=self._lead_fields(row, columns),
                )
            )

        # Duplicate groups: only the first row of each group is classified; the others copy its verdict
        duplicate_groups: list[list[EnrichmentLead]] = []
        rep_of: dict[int, int] = {}  # duplicate row index -> index of the group's classified row
        group_label: dict[int, str] = {}  # row index -> duplicate group number
        if self.groups_duplicates:
            by_dup_key: dict[str, list[EnrichmentLead]] = {}
            for lead in leads:
                dup_key = self._duplicate_key(lead)
                if dup_key:
                    by_dup_key.setdefault(dup_key, []).append(lead)
            duplicate_groups = [g for g in by_dup_key.values() if len(g) > 1]
            for number, group in enumerate(duplicate_groups, start=1):
                representative = self._duplicate_representative(group)
                for member in group:
                    group_label[member.index] = str(number)
                    if member is not representative:
                        rep_of[member.index] = representative.index
        work_leads = [lead for lead in leads if lead.index not in rep_of]

        key_to_leads: dict[str, list[EnrichmentLead]] = {}
        for lead in work_leads:
            if lead.crawl_key:
                key_to_leads.setdefault(lead.crawl_key, []).append(lead)

        telemetry = self.telemetry_cls(
            input_file=str(in_path),
            output_dir=str(out_dir),
            total_records=len(target_rows),
            duplicate_rows_removed=duplicates_removed,
            duplicate_groups=len(duplicate_groups),
            duplicate_rows=sum(len(g) for g in duplicate_groups),
            groups_duplicates=self.groups_duplicates,
            unique_websites=len(key_to_leads),
            summary_title=self.summary_title,
        )

        results: dict[int, R] = {}  # lead index -> verdict (classified rows of duplicate groups only)
        for lead in work_leads:
            if not lead.crawl_key:
                results[lead.index] = self._missing_url_result(lead)

        def group_done(key: str) -> bool:
            return all(lead.index in results for lead in key_to_leads[key])

        def group_needs_retry(key: str) -> bool:
            return any(lead.index in results and self._needs_retry(results[lead.index]) for lead in key_to_leads[key])

        # Resume from checkpoint (only entries recorded for this exact input file and classifier version)
        fingerprint = self._file_fingerprint(in_path)
        if resume and checkpoint_path.exists():
            try:
                with open(checkpoint_path, encoding="utf-8") as cf:
                    for line in cf:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        key = entry.get("key")
                        if entry.get("fingerprint") != fingerprint or key not in key_to_leads:
                            continue
                        # Verdicts from older classification rules are re-evaluated, never reused
                        if entry.get("classifier_version") != self.classifier_version:
                            continue
                        group = key_to_leads[key]
                        if "result" in entry:
                            shared = self._result_from_dict(entry["result"])
                            for lead in group:
                                results[lead.index] = shared
                        else:
                            per_lead = entry.get("results") or {}
                            for lead in group:
                                if str(lead.index) in per_lead:
                                    results[lead.index] = self._result_from_dict(per_lead[str(lead.index)])
            except Exception as exc:
                logger.debug(f"Could not resume from checkpoint: {exc}")
            # Partially restored groups are re-evaluated as a whole
            for key, group in key_to_leads.items():
                if not group_done(key) or (retry_unreachable and group_needs_retry(key)):
                    for lead in group:
                        results.pop(lead.index, None)
            telemetry.websites_from_checkpoint = sum(1 for k in key_to_leads if group_done(k))
        elif not resume and checkpoint_path.exists():
            checkpoint_path.unlink()

        cfg = getattr(self.crawler, "config", None)
        dup_note = (
            f"{len(duplicate_groups):,} duplicate groups covering {telemetry.duplicate_rows:,} rows"
            if self.groups_duplicates
            else f"{duplicates_removed} exact duplicates removed"
        )
        console.print(
            Panel(
                f"[bold white]{self.pipeline_title}[/bold white]\n"
                f"[cyan]Input CSV:[/cyan]  {in_path} ([bold yellow]{len(target_rows):,}[/bold yellow] rows, "
                f"{dup_note})\n"
                f"[cyan]Websites:[/cyan]   {len(key_to_leads):,} unique "
                f"({telemetry.websites_from_checkpoint:,} restored from checkpoint)\n"
                f"[cyan]Output Dir:[/cyan] {out_dir}\n"
                f"[cyan]Settings:[/cyan]   concurrency={self.concurrency_limit}, "
                f"retries={getattr(cfg, 'max_retries', '-')}, "
                f"robots.txt respected={getattr(cfg, 'respect_robots_txt', '-')}",
                border_style="blue",
            )
        )

        lock = asyncio.Lock()
        checkpoint_lock = asyncio.Lock()
        flush_state = {"last": time.time(), "since": 0}

        def lead_result(lead: EnrichmentLead) -> R | None:
            return results.get(rep_of.get(lead.index, lead.index))

        def build_out_row(lead: EnrichmentLead, res: R | None) -> list[str]:
            out_row = list(lead.row)
            while len(out_row) < len(output_headers):
                out_row.append("")
            for col, val in zip(enrichment_idx, self._enrichment_values(res), strict=True):
                out_row[col] = val
            if res is not None and columns.existing_class is not None and columns.existing_class < len(out_row):
                out_row[columns.existing_class] = self._decision_value(res)
            if res is not None:
                for col, val in self._input_column_values(res, columns).items():
                    if val and col < len(out_row):
                        out_row[col] = val
            return out_row

        def build_all_row(out_row: list[str], res: R | None) -> list[str]:
            all_row = list(out_row)
            while len(all_row) < len(all_headers):
                all_row.append("")
            if res is not None:
                all_row[niche_idx] = NICHE_VALUES.get(res.classification, "unreachable")
            return all_row

        def write_csv(dest: Path, rows: list[list[str]], headers: list[str] = output_headers) -> None:
            tmp = dest.with_suffix(".tmp")
            with open(tmp, mode="w", encoding="utf-8-sig", errors="replace", newline="") as wf:
                w = csv.writer(wf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
                w.writerow(headers)
                w.writerows(rows)
            tmp.replace(dest)

        # Qualified-sheet dedupe: rows of a duplicate group after its first occurrence
        later_duplicates: set[int] = (
            {m.index for g in duplicate_groups for m in g[1:]}
            if self.groups_duplicates and self.dedupe_qualified_sheet
            else set()
        )
        omitted_from_qualified: set[int] = set()  # refreshed on every flush

        def flush_all_csvs() -> dict[str, list[int]]:
            buckets: dict[str, list[list[str]]] = {cls: [] for cls in OUTPUT_FILENAMES}
            indexes: dict[str, list[int]] = {cls: [] for cls in OUTPUT_FILENAMES}
            all_rows: list[list[str]] = []
            omitted_from_qualified.clear()
            for lead in leads:
                res = lead_result(lead)
                out_row = build_out_row(lead, res)
                all_rows.append(build_all_row(out_row, res))
                if res is not None:
                    cls = res.classification if res.classification in OUTPUT_FILENAMES else "Unreachable"
                    if cls == "Yes" and lead.index in later_duplicates:
                        omitted_from_qualified.add(lead.index)  # its group's first row is already listed
                        continue
                    buckets[cls].append(out_row)
                    indexes[cls].append(lead.index)
            for cls, path in output_paths.items():
                write_csv(path, buckets[cls])
            write_csv(all_csv, all_rows, all_headers)
            if self.groups_duplicates:
                position = {lead.index: n for n, lead in enumerate(leads)}
                dup_rows: list[list[str]] = []
                for group in duplicate_groups:
                    classified_row = str(rep_of.get(group[0].index, group[0].index) + 2)
                    for member in group:
                        dup_rows.append(
                            [*all_rows[position[member.index]], group_label[member.index], str(len(group)), classified_row]
                        )
                write_csv(duplicates_csv, dup_rows, [*all_headers, *DUPLICATE_HEADERS])
            if reason_csv is not None:
                write_csv(
                    reason_csv,
                    [self._reason_report_row(lead, lead_result(lead), group_label.get(lead.index, "")) for lead in leads],
                    self._reason_report_headers(),
                )
            return indexes

        async def record(key: str, group_results: dict[int, R], url: str) -> None:
            async with checkpoint_lock:
                results.update(group_results)
                entry: dict[str, Any] = {
                    "fingerprint": fingerprint,
                    "classifier_version": self.classifier_version,
                    "key": key,
                    "url": url,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
                if self.shares_result_per_site:
                    entry["result"] = next(iter(group_results.values())).to_dict()
                else:
                    entry["results"] = {str(idx): r.to_dict() for idx, r in group_results.items()}
                with open(checkpoint_path, "a", encoding="utf-8") as cf:
                    cf.write(json.dumps(entry) + "\n")
                flush_state["since"] += 1
                now = time.time()
                if now - flush_state["last"] > 15.0 or flush_state["since"] >= 25:
                    flush_all_csvs()
                    flush_state["last"] = now
                    flush_state["since"] = 0

        # Initial flush so files exist immediately (restored results included)
        flush_all_csvs()

        async def run_pass(keys: list[str], crawler: WebsiteCrawler, concurrency: int, label: str) -> None:
            semaphore = asyncio.Semaphore(concurrency)
            row_total = sum(len(key_to_leads[k]) for k in keys)

            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(bar_width=30),
                TaskProgressColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task_id = progress.add_task(f"[cyan]{label}[/cyan]", total=row_total)

                async def process_key(key: str) -> None:
                    group = key_to_leads[key]
                    lead = group[0]
                    async with semaphore:
                        progress.update(task_id, description=f"[cyan]{label}: {lead.company[:26]}[/cyan]")
                        group_results = await self._safe_evaluate_group(crawler, group)
                    await record(key, group_results, lead.crawl_url)
                    res = group_results[lead.index]
                    async with lock:
                        color = {"Yes": "green", "No": "yellow", "Uncertain": "magenta"}.get(res.classification, "red")
                        suffix = f" (+{len(group) - 1} rows)" if len(group) > 1 else ""
                        progress.update(
                            task_id,
                            advance=len(group),
                            description=(
                                f"[{color}]{label}: {lead.company[:22]} -> {self._status_label(res)}{suffix}[/{color}]"
                            ),
                        )

                await asyncio.gather(*(process_key(k) for k in keys))

        # Pass 1: every unique website not already restored
        pending_keys = [k for k in key_to_leads if not group_done(k)]
        if pending_keys:
            await run_pass(pending_keys, self.crawler, self.concurrency_limit, "Crawling")

        # Pass 2: patient re-crawl of temporary failures and incomplete evaluations
        if second_pass:
            retry_keys = [k for k in key_to_leads if group_done(k) and group_needs_retry(k)]
            if retry_keys:
                before = {k: results[key_to_leads[k][0].index].classification for k in retry_keys}
                telemetry.retried_websites = len(retry_keys)
                console.print(
                    f"[bold cyan]Patient retry pass for {len(retry_keys)} website(s) with temporary failures...[/bold cyan]"
                )
                patient = self._make_patient_crawler()
                await run_pass(retry_keys, patient, max(2, self.concurrency_limit // 2), "Retrying")
                telemetry.recovered_unreachable_count = 0
                for k in retry_keys:
                    after = results[key_to_leads[k][0].index].classification
                    if (before[k] == "Unreachable" and after != "Unreachable") or (
                        before[k] == "Uncertain" and after in ("Yes", "No")
                    ):
                        telemetry.recovered_unreachable_count += 1
                if patient is not self.crawler and hasattr(patient, "aclose"):
                    await patient.aclose()

        if hasattr(self.crawler, "aclose"):
            try:
                await self.crawler.aclose()
            except Exception:
                pass

        # Final flush + statistics
        indexes = flush_all_csvs()
        for lead in leads:
            res = lead_result(lead)
            if res is None:
                continue
            telemetry.records_processed += 1
            if res.classification == "Yes":
                telemetry.qualified_count += 1
            elif res.classification == "No":
                telemetry.not_qualified_count += 1
            elif res.classification == "Uncertain":
                telemetry.uncertain_count += 1
            else:
                telemetry.unreachable_count += 1

        site_results = {k: results[g[0].index] for k, g in key_to_leads.items() if g[0].index in results}
        telemetry.websites_evaluated = sum(1 for r in site_results.values() if self._site_evaluated(r))
        telemetry.archived_evaluations = sum(
            1 for r in site_results.values() if r.website_status.startswith("Archived Copy")
        )
        for key, res in site_results.items():
            if self._site_unreachable(res):
                lead = key_to_leads[key][0]
                telemetry.persistent_failures.append(
                    {
                        "company": lead.company,
                        "url": lead.crawl_url,
                        "status": res.website_status,
                        "error": self._access_error(res)[:300],
                        "rows": str(len(key_to_leads[key])),
                    }
                )

        telemetry.qualified_duplicates_removed = len(omitted_from_qualified)
        telemetry.validation = self._validate_outputs(
            leads, indexes, output_paths, lead_result, omitted=set(omitted_from_qualified)
        )
        if self.groups_duplicates:
            with open(duplicates_csv, encoding="utf-8-sig", newline="") as f:
                dup_read_back = max(0, sum(1 for _ in csv.reader(f)) - 1)
            telemetry.validation["duplicates_sheet_rows"] = dup_read_back
            telemetry.validation["duplicate_rows_share_verdict"] = all(
                lead_result(m) is not None and lead_result(m) is lead_result(g[0]) for g in duplicate_groups for m in g
            )
            if dup_read_back != telemetry.duplicate_rows:
                telemetry.validation["passed"] = False
                telemetry.validation.setdefault("file_count_mismatches", []).append(
                    f"{DUPLICATES_FILENAME}: wrote {telemetry.duplicate_rows}, read back {dup_read_back}"
                )
        telemetry.output_files = {OUTPUT_FILENAMES[c]: str(p) for c, p in output_paths.items()}
        telemetry.output_files[ALL_LEADS_FILENAME] = str(all_csv)
        if self.groups_duplicates:
            telemetry.output_files[DUPLICATES_FILENAME] = str(duplicates_csv)
        if reason_csv is not None:
            telemetry.output_files[reason_csv.name] = str(reason_csv)
        telemetry.duration_seconds = time.time() - start_time

        self._write_report(report_path, telemetry, leads, lead_result)
        telemetry.print_summary()
        return telemetry

    async def _safe_evaluate_group(self, crawler: WebsiteCrawler, group: list[EnrichmentLead]) -> dict[int, R]:
        group_results = await self._evaluate_group(crawler, group)
        missing = [lead for lead in group if lead.index not in group_results]
        if missing:
            raise RuntimeError(f"{type(self).__name__}._evaluate_group returned no verdict for {len(missing)} lead(s)")
        return group_results

    # ------------------------------------------------------------------ QA
    def _validate_outputs(
        self,
        leads: list[EnrichmentLead],
        indexes: dict[str, list[int]],
        output_paths: dict[str, Path],
        lead_result: Callable[[EnrichmentLead], R | None],
        omitted: set[int] | None = None,
    ) -> dict[str, Any]:
        """Verify each lead appears in exactly one output, files round-trip, and qualified leads carry evidence.

        `omitted`: rows deliberately left out of the category sheets (duplicates removed from qualified_leads.csv).
        """
        expected = {lead.index for lead in leads} - (omitted or set())
        placed: dict[int, list[str]] = {}
        for cls, idx_list in indexes.items():
            for idx in idx_list:
                placed.setdefault(idx, []).append(cls)
        missing = sorted(expected - set(placed))
        in_multiple = sorted(i for i, c in placed.items() if len(c) > 1)

        file_counts: dict[str, int] = {}
        file_count_mismatch: list[str] = []
        for cls, path in output_paths.items():
            with open(path, encoding="utf-8-sig", newline="") as f:
                n = max(0, sum(1 for _ in csv.reader(f)) - 1)
            file_counts[path.name] = n
            if n != len(indexes[cls]):
                file_count_mismatch.append(f"{path.name}: wrote {len(indexes[cls])}, read back {n}")

        qualified_without_evidence = [
            lead.company
            for lead in leads
            if (r := lead_result(lead)) is not None and r.classification == "Yes" and not self._has_evidence(r)
        ]

        passed = not missing and not in_multiple and not file_count_mismatch and not qualified_without_evidence
        return {
            "passed": passed,
            "leads_expected": len(expected),
            "leads_placed": len(placed),
            "missing_from_outputs": missing,
            "in_multiple_outputs": in_multiple,
            "file_row_counts": file_counts,
            "file_count_mismatches": file_count_mismatch,
            "qualified_without_evidence": qualified_without_evidence,
            **self._extra_validation(leads, lead_result),
        }

    def _write_report(
        self,
        report_path: Path,
        telemetry: NicheEnrichmentTelemetry,
        leads: list[EnrichmentLead],
        lead_result: Callable[[EnrichmentLead], R | None],
    ) -> None:
        uncertain = [
            {"company": lead.company, "url": lead.crawl_url, "reason": r.reasoning[:300]}
            for lead in leads
            if (r := lead_result(lead)) is not None and r.classification == "Uncertain"
        ]
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "classifier_version": self.classifier_version,
            "input_file": telemetry.input_file,
            "output_dir": telemetry.output_dir,
            "totals": {
                "input_rows": telemetry.total_records,
                "exact_duplicate_rows_removed": telemetry.duplicate_rows_removed,
                "duplicate_groups": telemetry.duplicate_groups,
                "rows_in_duplicate_groups": telemetry.duplicate_rows,
                "duplicates_removed_from_qualified_sheet": telemetry.qualified_duplicates_removed,
                "leads_classified": telemetry.records_processed,
                "qualified": telemetry.qualified_count,
                "not_qualified": telemetry.not_qualified_count,
                "uncertain": telemetry.uncertain_count,
                "unreachable": telemetry.unreachable_count,
                "qualification_rate_pct": round(telemetry.qualification_rate, 2),
                "unique_websites": telemetry.unique_websites,
                "websites_successfully_evaluated": telemetry.websites_evaluated,
                "websites_requiring_verification": telemetry.uncertain_count + telemetry.unreachable_count,
                "websites_retried": telemetry.retried_websites,
                "recovered_on_retry": telemetry.recovered_unreachable_count,
                "restored_from_checkpoint": telemetry.websites_from_checkpoint,
                "evaluated_via_archived_snapshot": telemetry.archived_evaluations,
            },
            "output_files": telemetry.output_files,
            "validation": telemetry.validation,
            "persistent_crawl_failures": telemetry.persistent_failures,
            "uncertain_cases": uncertain,
            "duration_seconds": round(telemetry.duration_seconds, 1),
        }
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        except Exception as exc:
            logger.debug(f"Could not write run report: {exc}")
