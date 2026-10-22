"""Production-grade Christmas, Holiday & Permanent Decorative Light Installation lead classification pipeline.

Reads contractor leads from CSV, crawls each company website thoroughly (homepage, navigation,
sitemap, holiday/permanent-lighting service pages), classifies genuine installation service
providers vs. product sellers and unrelated trades, and writes four segregated output CSVs with
enrichment columns. 100% preserves original input data, columns, multi-line values, and row order.

Reliability features:
- Each unique website is crawled once (rows sharing a domain reuse the verdict).
- Bounded global concurrency plus per-domain request limits and delays (inside the crawler).
- Results are checkpointed incrementally (JSONL) so interrupted runs resume without re-crawling.
- A second, more patient pass re-crawls websites that failed with temporary errors.
- Post-run QA validates that every input lead appears in exactly one output file.
"""

import asyncio
import csv
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
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

from contractor_scraper.classification.christmas_light_classifier import (
    CLASSIFIER_VERSION,
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.crawler import (
    SOCIAL_PROFILE_DOMAINS,
    TRANSIENT_STATUS_CATEGORIES,
    CrawlerConfig,
    CrawlResult,
    WebsiteCrawler,
)

logger = get_logger("christmas_light_enricher")
console = Console()

# Live-site failures for which a public archive snapshot is consulted (the site exists but could not be read)
ARCHIVE_FALLBACK_CATEGORIES: frozenset[str] = TRANSIENT_STATUS_CATEGORIES | frozenset(
    {"Bot Challenge", "HTTP Error 401", "HTTP Error 403", "HTTP Error 406", "HTTP Error 451", "Redirect Loop"}
)
MAX_ARCHIVE_AGE_YEARS = 4

HOLIDAY_PRIORITY_KEYWORDS: list[str] = [
    "/christmas",
    "/holiday",
    "/holiday-lighting",
    "/christmas-lights",
    "/permanent-lighting",
    "/seasonal-lighting",
    "/lighting",
    "/services",
    "/outdoor-lighting",
    "/exterior-lighting",
    "/residential",
    "/commercial",
    "/gallery",
    "/faq",
    "/about",
    "/contact",
    "/quote",
    "/estimate",
]

# Output files (written inside the Christmas Light Installation output folder)
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
CHECKPOINT_FILENAME = "crawl_checkpoint.jsonl"
REPORT_FILENAME = "run_report.json"

ENRICHMENT_HEADERS: list[str] = [
    "Qualification_Status",  # Qualified | Rejected | Needs Review | Unverified
    "Qualified_Niches",
    "Christmas_Light_Installation",
    "Holiday_Light_Installation",
    "Permanent_Decorative_Lighting_Installation",
    "Roofline_Lighting_Installation",
    "Evidence_URLs",
    "Evidence_Summary",
    "Service_Keywords_Found",
    "Crawl_Status",
    "Classification_Confidence",
]

# Hosted-platform roots where only the full profile path identifies the business
PROFILE_PLATFORM_HOSTS: frozenset[str] = SOCIAL_PROFILE_DOMAINS | frozenset({"sites.google.com"})


def christmas_crawler_config(read_timeout: float = 20.0) -> CrawlerConfig:
    """Patient, thorough crawl profile for holiday-lighting qualification."""
    read_timeout = max(5.0, read_timeout)
    return CrawlerConfig(
        connect_timeout=min(15.0, max(8.0, read_timeout * 0.6)),
        read_timeout=read_timeout,
        domain_timeout=max(120.0, read_timeout * 7.0),
        max_retries=3,
        backoff_base=2.0,
        backoff_max=20.0,
        per_domain_concurrency=3,
        request_delay=0.35,
        max_deep_pages=6,
        max_probe_pages=6,
        respect_robots_txt=True,
        use_sitemap=True,
        max_sitemap_pages=5,
        impersonate_search_bot_on_challenge=False,
        render_javascript=True,
        block_separators=True,
        # v3: page text now keeps block separators, so v2 cache entries are not comparable
        cache_namespace="crawler_christmas_v3",
    )


@dataclass
class ChristmasEnrichmentTelemetry:
    """Execution telemetry and metrics for Christmas Light Installation lead classification."""

    input_file: str
    output_dir: str
    total_records: int = 0
    duplicate_rows_removed: int = 0
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

    @property
    def qualification_rate(self) -> float:
        return (self.qualified_count / self.records_processed * 100.0) if self.records_processed else 0.0

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
class _Lead:
    index: int
    row: list[str]
    company: str
    crawl_url: str
    crawl_key: str


class ChristmasLightLeadEnricher:
    """Manages reading, crawling, classifying, checkpointing, and saving Christmas light contractor leads."""

    def __init__(
        self,
        classifier: ChristmasLightClassifier | None = None,
        crawler: WebsiteCrawler | None = None,
        concurrency_limit: int = 8,
        timeout_seconds: float = 20.0,
        crawler_config: CrawlerConfig | None = None,
        max_subpages: int = 10,
        archive_fallback: bool = True,
    ) -> None:
        self.archive_fallback = archive_fallback
        self.classifier = classifier or ChristmasLightClassifier()
        self.crawler_config = crawler_config or christmas_crawler_config(timeout_seconds)
        self.max_subpages = max_subpages
        self.crawler = crawler or WebsiteCrawler(
            timeout_seconds=timeout_seconds,
            max_subpages=max_subpages,
            priority_keywords=HOLIDAY_PRIORITY_KEYWORDS,
            config=self.crawler_config,
        )
        self.concurrency_limit = max(1, concurrency_limit)

    # ------------------------------------------------------------------ input handling
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

    # ------------------------------------------------------------------ crawling
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

    async def _evaluate(
        self, crawler: WebsiteCrawler, url: str, company: str
    ) -> ChristmasLightClassificationResult:
        try:
            crawl_res: CrawlResult = await crawler.crawl_domain(url)
            result = self.classifier.evaluate_crawl(crawl_res, company_name=company)
            if (
                result.classification == "Unreachable"
                and self.archive_fallback
                and type(crawler) is WebsiteCrawler
                and crawl_res.status_category in ARCHIVE_FALLBACK_CATEGORIES
            ):
                archived = await crawler.crawl_archived(url)
                if archived is not None:
                    return self._classify_archived(archived, crawl_res, result, company)
            return result
        except Exception as exc:
            logger.debug(f"Error evaluating {url}: {exc}")
            return ChristmasLightClassificationResult(
                classification="Unreachable",
                decision_label="Uncertain",
                confidence=0.0,
                website_status="Connection Error",
                reasoning=f"Unexpected error visiting {url}: {exc}",
                access_error=str(exc),
            )

    def _classify_archived(
        self,
        archived: CrawlResult,
        live: CrawlResult,
        live_result: ChristmasLightClassificationResult,
        company: str,
    ) -> ChristmasLightClassificationResult:
        """Classify an archived snapshot, discounting confidence and refusing to decide on stale snapshots."""
        res = self.classifier.evaluate_crawl(archived, company_name=company)
        ts = archived.archived_snapshot
        snap_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else "unknown date"
        age_years = datetime.now().year - int(ts[:4]) if ts[:4].isdigit() else 99
        prefix = (
            f"Live site unavailable ({live.status_category}: {(live.error_message or '')[:120]}); "
            f"evaluated Wayback Machine snapshot from {snap_date}. "
        )
        res.website_status = f"Archived Copy (live: {live.status_category})"
        res.access_error = live_result.access_error
        res.confidence = round(res.confidence * 0.9, 3)

        if res.classification in ("Yes", "No") and age_years > MAX_ARCHIVE_AGE_YEARS:
            verdict = "showed" if res.classification == "Yes" else "did not show"
            res.classification = "Uncertain"
            res.decision_label = "Uncertain"
            res.confidence = 0.35
            res.reasoning = (
                f"{prefix}The most recent usable snapshot is over {MAX_ARCHIVE_AGE_YEARS} years old and {verdict} "
                f"qualifying services; current offering needs verification. Archived finding: {res.reasoning}"
            )
            return res
        if res.classification == "Unreachable":
            return live_result
        res.reasoning = prefix + res.reasoning
        return res

    @staticmethod
    def _needs_retry(res: ChristmasLightClassificationResult) -> bool:
        if res.website_status.startswith("Archived Copy"):
            return any(f"live: {cat}" in res.website_status for cat in TRANSIENT_STATUS_CATEGORIES)
        if res.classification == "Unreachable":
            return res.website_status in TRANSIENT_STATUS_CATEGORIES
        if res.classification == "Uncertain":
            return "incomplete" in (res.crawl_details or "") or "could not be evaluated" in res.reasoning
        return False

    # ------------------------------------------------------------------ output
    @staticmethod
    def _crawl_status(res: ChristmasLightClassificationResult) -> str:
        status = res.website_status or ""
        if res.crawl_details:
            status = f"{status} | {res.crawl_details}"
        if res.classification == "Unreachable" and res.access_error:
            status = f"{status} | {res.access_error[:300]}"
        return status

    def _enrichment_values(self, res: ChristmasLightClassificationResult | None) -> list[str]:
        if res is None:
            return ["Pending"] + [""] * (len(ENRICHMENT_HEADERS) - 1)
        return [
            res.qualification_status,
            "; ".join(res.qualified_niches),
            res.christmas_service,
            res.holiday_service,
            res.permanent_service,
            res.roofline_service,
            " | ".join(res.evidence_urls),
            res.reasoning,
            "; ".join(res.keywords_found or res.evidence_phrases),
            self._crawl_status(res),
            f"{res.confidence:.2f}",
        ]

    # ------------------------------------------------------------------ main pipeline
    async def process_leads(
        self,
        input_csv_path: str | Path,
        output_dir: str | Path | None = None,
        limit: int | None = None,
        resume: bool = True,
        retry_unreachable: bool = True,
        second_pass: bool = True,
    ) -> ChristmasEnrichmentTelemetry:
        """Classify contractor leads in the input CSV and write 4 categorized output CSV files."""
        start_time = time.time()
        # Per-request HTTP logs would drown the progress display
        logging.getLogger("httpx").setLevel(logging.WARNING)
        in_path = Path(input_csv_path)
        if not in_path.exists():
            raise FileNotFoundError(f"Input CSV file not found: {in_path}")
        out_dir = Path(output_dir) if output_dir else in_path.parent / "output" / in_path.stem
        out_dir.mkdir(parents=True, exist_ok=True)

        output_paths = {cls: out_dir / name for cls, name in OUTPUT_FILENAMES.items()}
        all_csv = out_dir / ALL_LEADS_FILENAME
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

        web_idx, name_idx, existing_class_idx, base_headers, raw_web_idx, alt_name_idx = self._identify_columns(raw_headers)

        # Output header layout: original headers + enrichment columns (reused if already present)
        output_headers = list(base_headers)
        for eh in ENRICHMENT_HEADERS:
            if eh not in output_headers:
                output_headers.append(eh)
        enrichment_idx = [output_headers.index(eh) for eh in ENRICHMENT_HEADERS]
        # all_leads.csv layout: the shared layout plus the niche column
        all_headers = [*output_headers, NICHE_COLUMN]
        niche_idx = len(output_headers)

        target_rows = all_raw_rows[:limit] if limit is not None else all_raw_rows

        # Build lead list, dropping exact duplicate rows
        leads: list[_Lead] = []
        seen_rows: set[tuple[str, ...]] = set()
        duplicates_removed = 0
        for i, row in enumerate(target_rows):
            row_key = tuple(c.strip() for c in row)
            if row_key in seen_rows:
                duplicates_removed += 1
                continue
            seen_rows.add(row_key)

            def cell(idx: int | None, r: list[str] = row) -> str:
                return r[idx].strip() if idx is not None and idx < len(r) else ""

            company = cell(name_idx) or cell(alt_name_idx)
            crawl_url = self._select_crawl_url(cell(web_idx), cell(raw_web_idx))
            lead = _Lead(index=i, row=list(row), company=company, crawl_url=crawl_url, crawl_key=self._crawl_key(crawl_url) if crawl_url else "")
            leads.append(lead)

        key_to_leads: dict[str, list[_Lead]] = {}
        for lead in leads:
            if lead.crawl_key:
                key_to_leads.setdefault(lead.crawl_key, []).append(lead)

        telemetry = ChristmasEnrichmentTelemetry(
            input_file=str(in_path),
            output_dir=str(out_dir),
            total_records=len(target_rows),
            duplicate_rows_removed=duplicates_removed,
            unique_websites=len(key_to_leads),
        )

        results_by_key: dict[str, ChristmasLightClassificationResult] = {}
        missing_result = ChristmasLightClassificationResult(
            classification="Uncertain",
            decision_label="Uncertain",
            confidence=0.0,
            website_status="Missing URL",
            reasoning="Website URL is empty or invalid in the input record; cannot verify services.",
        )

        # Resume from checkpoint (only entries recorded for this exact input file)
        fingerprint = self._file_fingerprint(in_path)
        if resume and checkpoint_path.exists():
            try:
                with open(checkpoint_path, encoding="utf-8") as cf:
                    for line in cf:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("fingerprint") != fingerprint or entry.get("key") not in key_to_leads:
                            continue
                        # Verdicts from older classification rules are re-evaluated, never reused
                        if entry.get("classifier_version") != CLASSIFIER_VERSION:
                            continue
                        results_by_key[entry["key"]] = ChristmasLightClassificationResult.from_dict(entry["result"])
            except Exception as exc:
                logger.debug(f"Could not resume from checkpoint: {exc}")
            if retry_unreachable:
                for k in [k for k, r in results_by_key.items() if self._needs_retry(r)]:
                    del results_by_key[k]
            telemetry.websites_from_checkpoint = len(results_by_key)
        elif not resume and checkpoint_path.exists():
            checkpoint_path.unlink()

        console.print(
            Panel(
                f"[bold white]Christmas / Holiday / Permanent Lighting Lead Classification Engine[/bold white]\n"
                f"[cyan]Input CSV:[/cyan]  {in_path} ([bold yellow]{len(target_rows):,}[/bold yellow] rows, "
                f"{duplicates_removed} exact duplicates removed)\n"
                f"[cyan]Websites:[/cyan]   {len(key_to_leads):,} unique "
                f"({telemetry.websites_from_checkpoint:,} restored from checkpoint)\n"
                f"[cyan]Output Dir:[/cyan] {out_dir}\n"
                f"[cyan]Settings:[/cyan]   concurrency={self.concurrency_limit}, "
                f"retries={getattr(getattr(self.crawler, 'config', None), 'max_retries', '-')}, "
                f"robots.txt respected={getattr(getattr(self.crawler, 'config', None), 'respect_robots_txt', '-')}",
                border_style="blue",
            )
        )

        lock = asyncio.Lock()
        checkpoint_lock = asyncio.Lock()
        flush_state = {"last": time.time(), "since": 0}

        def lead_result(lead: _Lead) -> ChristmasLightClassificationResult | None:
            if not lead.crawl_key:
                return missing_result
            return results_by_key.get(lead.crawl_key)

        def build_out_row(lead: _Lead, res: ChristmasLightClassificationResult | None) -> list[str]:
            out_row = list(lead.row)
            while len(out_row) < len(output_headers):
                out_row.append("")
            for col, val in zip(enrichment_idx, self._enrichment_values(res), strict=True):
                out_row[col] = val
            if res is not None and existing_class_idx is not None and existing_class_idx < len(out_row):
                out_row[existing_class_idx] = res.decision_label
            return out_row

        def build_all_row(out_row: list[str], res: ChristmasLightClassificationResult | None) -> list[str]:
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

        def flush_all_csvs() -> dict[str, list[int]]:
            buckets: dict[str, list[list[str]]] = {cls: [] for cls in OUTPUT_FILENAMES}
            indexes: dict[str, list[int]] = {cls: [] for cls in OUTPUT_FILENAMES}
            all_rows: list[list[str]] = []
            for lead in leads:
                res = lead_result(lead)
                out_row = build_out_row(lead, res)
                all_rows.append(build_all_row(out_row, res))
                if res is not None:
                    cls = res.classification if res.classification in OUTPUT_FILENAMES else "Unreachable"
                    buckets[cls].append(out_row)
                    indexes[cls].append(lead.index)
            for cls, path in output_paths.items():
                write_csv(path, buckets[cls])
            write_csv(all_csv, all_rows, all_headers)
            return indexes

        async def record(key: str, res: ChristmasLightClassificationResult, url: str) -> None:
            async with checkpoint_lock:
                results_by_key[key] = res
                with open(checkpoint_path, "a", encoding="utf-8") as cf:
                    cf.write(
                        json.dumps(
                            {
                                "fingerprint": fingerprint,
                                "classifier_version": CLASSIFIER_VERSION,
                                "key": key,
                                "url": url,
                                "ts": datetime.now().isoformat(timespec="seconds"),
                                "result": res.to_dict(),
                            }
                        )
                        + "\n"
                    )
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
                        res = await self._evaluate(crawler, lead.crawl_url, lead.company)
                    await record(key, res, lead.crawl_url)
                    async with lock:
                        color = {"Yes": "green", "No": "yellow", "Uncertain": "magenta"}.get(res.classification, "red")
                        progress.update(
                            task_id,
                            advance=len(group),
                            description=f"[{color}]{label}: {lead.company[:22]} -> {res.qualification_status}[/{color}]",
                        )

                await asyncio.gather(*(process_key(k) for k in keys))

        # Pass 1: every unique website not already restored
        pending_keys = [k for k in key_to_leads if k not in results_by_key]
        if pending_keys:
            await run_pass(pending_keys, self.crawler, self.concurrency_limit, "Crawling")

        # Pass 2: patient re-crawl of temporary failures and incomplete evaluations
        if second_pass:
            retry_keys = [k for k in key_to_leads if k in results_by_key and self._needs_retry(results_by_key[k])]
            if retry_keys:
                before = {k: results_by_key[k].classification for k in retry_keys}
                telemetry.retried_websites = len(retry_keys)
                console.print(f"[bold cyan]Patient retry pass for {len(retry_keys)} website(s) with temporary failures...[/bold cyan]")
                patient = self._make_patient_crawler()
                await run_pass(retry_keys, patient, max(2, self.concurrency_limit // 2), "Retrying")
                telemetry.recovered_unreachable_count = sum(
                    1
                    for k in retry_keys
                    if (before[k] == "Unreachable" and results_by_key[k].classification != "Unreachable")
                    or (before[k] == "Uncertain" and results_by_key[k].classification in ("Yes", "No"))
                )
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

        telemetry.websites_evaluated = sum(
            1 for r in results_by_key.values() if r.classification in ("Yes", "No", "Uncertain") and r.website_status not in ("Missing URL",)
        )
        telemetry.archived_evaluations = sum(1 for r in results_by_key.values() if r.website_status.startswith("Archived Copy"))
        for key, res in results_by_key.items():
            if res.classification == "Unreachable":
                lead = key_to_leads[key][0]
                telemetry.persistent_failures.append(
                    {
                        "company": lead.company,
                        "url": lead.crawl_url,
                        "status": res.website_status,
                        "error": (res.access_error or res.reasoning)[:300],
                        "rows": str(len(key_to_leads[key])),
                    }
                )

        telemetry.validation = self._validate_outputs(leads, indexes, output_paths, lead_result)
        telemetry.output_files = {OUTPUT_FILENAMES[c]: str(p) for c, p in output_paths.items()}
        telemetry.output_files[ALL_LEADS_FILENAME] = str(all_csv)
        telemetry.duration_seconds = time.time() - start_time

        self._write_report(report_path, telemetry, leads, lead_result)
        telemetry.print_summary()
        return telemetry

    # ------------------------------------------------------------------ QA
    def _validate_outputs(
        self,
        leads: list[_Lead],
        indexes: dict[str, list[int]],
        output_paths: dict[str, Path],
        lead_result: Any,
    ) -> dict[str, Any]:
        """Verify each lead appears in exactly one output, files round-trip, and qualified leads carry evidence."""
        expected = {lead.index for lead in leads}
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
            if (r := lead_result(lead)) is not None and r.classification == "Yes" and not (r.evidence_urls and r.reasoning)
        ]
        single_page_rejections = [
            lead.company
            for lead in leads
            if (r := lead_result(lead)) is not None
            and r.classification == "No"
            and r.pages_evaluated <= 1
            and "national retail" not in r.reasoning
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
            "not_qualified_single_page_decisions": len(single_page_rejections),
        }

    def _write_report(
        self, report_path: Path, telemetry: ChristmasEnrichmentTelemetry, leads: list[_Lead], lead_result: Any
    ) -> None:
        uncertain = [
            {"company": lead.company, "url": lead.crawl_url, "reason": r.reasoning[:300]}
            for lead in leads
            if (r := lead_result(lead)) is not None and r.classification == "Uncertain"
        ]
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "input_file": telemetry.input_file,
            "output_dir": telemetry.output_dir,
            "totals": {
                "input_rows": telemetry.total_records,
                "exact_duplicate_rows_removed": telemetry.duplicate_rows_removed,
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
