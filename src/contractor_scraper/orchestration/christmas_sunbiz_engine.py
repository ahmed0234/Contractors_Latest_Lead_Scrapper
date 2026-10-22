"""Sunbiz -> Christmas / holiday lighting installation lead engine.

Pipeline (one candidate at a time per worker, leads persisted the moment they are verified):
1. Scan Florida Sunbiz daily filing files (local cache by default) and pre-filter registrations by
   name (`classification.christmas_name_filter`), ranking niche-named companies first.
2. Discover candidate websites (`ChristmasWebsiteFinder`: synthetic domain probes, optional search
   APIs, Bing, DuckDuckGo, Yahoo, directory outbound links).
3. Crawl each candidate with the patient Christmas crawl profile and keep it only when
   `verify_company_identity` ties the site to this exact registration.
4. Classify the verified site with `ChristmasLightClassifier`; only "Yes" verdicts with confirmed
   Christmas / holiday / permanent-holiday installation evidence are written as leads. "Uncertain"
   and below-threshold verdicts go to a separate review CSV.
5. Pick a publicly listed email from the verified site (company domain or a free-mail address shown on
   it; third-party domains are ignored) and sanity-check its mail domain. Nothing is ever guessed.
6. Append the row to the CSV immediately (flush + fsync), journal the outcome for resume, move on.

Outputs (next to the leads CSV):
    <stem>.csv               qualified leads (company_name, email, website, formation_date, ...)
    <stem>_review.csv        evidence too weak to accept automatically
    <stem>_state.jsonl       per-registration outcome journal used to resume interrupted runs
    <stem>_run_summary.json  counts for the last run
"""

import asyncio
import csv
import dataclasses
import json
import os
import re
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from contractor_scraper.acquisition.sunbiz import FloridaSunbizSource
from contractor_scraper.classification.christmas_light_classifier import (
    SERVICE_FIELD_YES,
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.classification.christmas_name_filter import (
    SCOPE_FOCUSED,
    evaluate_company_name,
    is_non_profit_filing,
)
from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import get_logger
from contractor_scraper.core.progress import console
from contractor_scraper.discovery.christmas_website_finder import ChristmasWebsiteFinder
from contractor_scraper.discovery.company_identity import (
    IdentityMatch,
    normalize_text,
    verify_company_identity,
)
from contractor_scraper.discovery.crawler import (
    KNOWN_NON_CONTRACTOR_DOMAINS,
    CrawlResult,
    WebsiteCrawler,
)
from contractor_scraper.discovery.website_finder import (
    BLOCKED_DIRECTORY_DOMAINS,
    extract_root_domain,
)
from contractor_scraper.extraction.email_extractor import EmailExtractor
from contractor_scraper.extraction.mail_domain import MAIL_NO_HOST, MailDomainChecker
from contractor_scraper.models.email import EMAIL_REGEX
from contractor_scraper.normalization.cleaners import clean_company_name
from contractor_scraper.orchestration.christmas_light_enricher import christmas_crawler_config
from contractor_scraper.parsing.sunbiz_parser import SunbizRecordParser

logger = get_logger("christmas_sunbiz_engine")

LEAD_COLUMNS: list[str] = [
    "company_name",
    "email",
    "website",
    "formation_date",
    "sunbiz_document_number",
    "city",
    "state",
    "service_category",
    "verification_status",
    "identity_confidence",
    "classification_confidence",
    "evidence_url",
    "evidence_summary",
    "discovery_source",
    "email_source",
    "email_check",
    "candidate_tier",
    "processed_at",
]

DEFAULT_OUTPUT_NAME = "christmas_holiday_lighting_leads.csv"
CRAWL_CACHE_NAMESPACE = "crawler_christmas_sunbiz_v1"
JOURNAL_VERSION = 1

# Outcome statuses
QUALIFIED = "qualified"
REVIEW = "needs_review"
NOT_QUALIFIED = "not_qualified"
NO_WEBSITE = "no_website_found"
WEBSITE_UNVERIFIED = "website_not_verified"
WEBSITE_UNREACHABLE = "website_unreachable"
SEARCH_UNAVAILABLE = "search_unavailable"
DUPLICATE = "duplicate"
TIMEOUT = "timeout"
ERROR = "error"

# Re-attempted on later runs (up to max_attempts); everything else is final
RETRYABLE_STATUSES = frozenset({WEBSITE_UNREACHABLE, SEARCH_UNAVAILABLE, TIMEOUT, ERROR})

# Quick pre-check on the raw name slice before full parsing (superset of every name-filter tier)
_NAME_PRECHECK = re.compile(
    r"CHRISTMAS|XMAS|X-MAS|YULE|TRIMLIGHT|LIGHT|LITE|ILLUMIN|GLOW|DECOR|DISPLAY|HOLIDAY|"
    r"WASH|WINDOW|GUTTER|LANDSCAP|LAWN|ROOFING|ELECTRIC|EXTERIOR|OUTDOOR",
)


# --------------------------------------------------------------------------------------------- models
@dataclass
class SunbizCandidate:
    """A Sunbiz registration selected for website research."""

    document_number: str
    legal_name: str
    search_name: str
    city: str
    state: str
    zip_code: str
    street_address: str
    formation_date: str
    status: str
    filing_type: str
    tier: int
    matched_term: str
    source_file: str = ""


@dataclass
class CandidateOutcome:
    status: str
    reason: str
    row: dict[str, str] | None = None
    website: str = ""

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES


@dataclass
class ChristmasSunbizConfig:
    """Runtime options for the engine (all exposed on the CLI)."""

    output_csv: Path | None = None
    years: list[int] | None = None
    local_only: bool = True
    max_files: int | None = None
    scope: str = SCOPE_FOCUSED
    include_inactive: bool = False
    limit: int | None = None  # max registrations researched this run
    max_leads: int | None = None  # stop after this many new qualified leads
    concurrency: int = 6
    timeout: float = 15.0  # per-request read timeout for site crawls
    candidate_timeout: float = 420.0  # whole research budget per registration
    max_candidate_sites: int = 3
    search_interval: float = 1.5
    min_classification_confidence: float = 0.0
    resume: bool = True
    max_attempts: int = 3
    check_mail_domains: bool = True


@dataclass
class EngineSummary:
    output_csv: str
    review_csv: str
    state_file: str
    started_at: str
    finished_at: str = ""
    duration_seconds: float = 0.0
    files_scanned: int = 0
    records_scanned: int = 0
    candidates_found: int = 0
    skipped_already_processed: int = 0
    researched: int = 0
    new_leads: int = 0
    new_review: int = 0
    leads_with_email: int = 0
    total_leads_in_csv: int = 0
    pending_unwritten_rows: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    stopped_reason: str = ""


# ---------------------------------------------------------------------------------------- helpers
def format_formation_date(raw: str) -> str:
    """Sunbiz MMDDYYYY -> YYYY-MM-DD; anything unparseable stays empty (never invented)."""
    raw = (raw or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return ""
    try:
        return datetime.strptime(raw, "%m%d%Y").date().isoformat()
    except ValueError:
        return ""


def company_key(name: str) -> str:
    return normalize_text(clean_company_name(name))


def _is_excluded_domain(domain: str) -> bool:
    domain = domain.lower()
    for blocked in BLOCKED_DIRECTORY_DOMAINS:
        if domain == blocked or domain.endswith("." + blocked):
            return True
    return any(ncd in domain for ncd in KNOWN_NON_CONTRACTOR_DOMAINS)


def _site_root(url: str) -> str:
    """scheme://host of the page the site actually answered on (keeps http-only and www hosts)."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return f"{parsed.scheme or 'https'}://{parsed.netloc.lower()}" if parsed.netloc else url


def candidate_from_line(
    line: str,
    parser: SunbizRecordParser,
    scope: str = SCOPE_FOCUSED,
    include_inactive: bool = False,
    source_file: str = "",
) -> SunbizCandidate | None:
    """Parse one fixed-width Sunbiz line into a research candidate, or None when out of scope."""
    if len(line) < 332 or not _NAME_PRECHECK.search(line[12:204].upper()):
        return None
    parsed = parser.parse_line(line)
    if not parsed:
        return None
    meta = parsed.raw_metadata or {}
    if not include_inactive and not meta.get("is_active", True):
        return None
    if is_non_profit_filing(meta.get("filing_type", "")):
        return None
    verdict = evaluate_company_name(parsed.raw_name, scope)
    if verdict is None:
        return None
    search_name = clean_company_name(parsed.raw_name)
    if not search_name or not parsed.source_record_id:
        return None
    return SunbizCandidate(
        document_number=parsed.source_record_id.strip().upper(),
        legal_name=" ".join(parsed.raw_name.split()),
        search_name=search_name,
        city=(parsed.raw_city or "").strip().title(),
        state=(parsed.raw_state or "FL").strip().upper(),
        zip_code=(meta.get("zip_code") or "").strip(),
        street_address=(meta.get("street_address") or "").strip(),
        formation_date=format_formation_date(meta.get("file_date", "")),
        status="Active" if meta.get("status") == "A" else (meta.get("status") or ""),
        filing_type=meta.get("filing_type", ""),
        tier=verdict.tier,
        matched_term=verdict.matched,
        source_file=source_file,
    )


def qualifies_for_niche(result: ChristmasLightClassificationResult) -> bool:
    """Accept only confirmed Christmas / holiday installation (permanent lighting counts only with holiday context)."""
    if not result.is_qualified:
        return False
    return SERVICE_FIELD_YES in (result.christmas_service, result.holiday_service, result.permanent_service)


# ---------------------------------------------------------------------------------- persistence
class LeadFileFormatError(RuntimeError):
    """Raised when an existing output CSV has a different header than this engine writes."""


class IncrementalCsvWriter:
    """Appends one row at a time and forces it to disk; rows blocked by a file lock wait in memory.

    Excel on Windows locks open CSV files. When that happens rows are kept in `pending` and written
    on the next append (or at shutdown), so nothing is lost while the file is open elsewhere.
    """

    def __init__(self, path: Path, columns: list[str]) -> None:
        self.path = path
        self.columns = columns
        self.pending: list[dict[str, str]] = []
        self._lock_warned = False

    def ensure_header(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            with open(self.path, encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            if header != self.columns:
                raise LeadFileFormatError(
                    f"{self.path} exists with different columns {header[:6]}... - "
                    "rename it or pass a different --output path."
                )
            return
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(self.columns)
            f.flush()
            os.fsync(f.fileno())

    def read_rows(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def _write(self, rows: list[dict[str, str]]) -> None:
        with open(self.path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.columns, extrasaction="ignore")
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in self.columns})
            f.flush()
            os.fsync(f.fileno())

    def append(self, row: dict[str, str]) -> bool:
        """Write `row` (after any rows still pending). Returns False if it had to stay pending."""
        self.pending.append(row)
        return self.flush_pending()

    def flush_pending(self) -> bool:
        if not self.pending:
            return True
        try:
            self._write(self.pending)
        except PermissionError:
            if not self._lock_warned:
                console.print(
                    f"[bold yellow]  {self.path.name} is locked (open in Excel?). "
                    "Leads are kept and will be written as soon as it is closed.[/bold yellow]"
                )
                self._lock_warned = True
            return False
        self.pending.clear()
        self._lock_warned = False
        return True


class ProcessingJournal:
    """Append-only JSONL record of every researched registration (the resume source of truth)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        if not self.path.exists():
            return entries
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a line cut off by a crash
                doc = entry.get("doc")
                if doc:
                    entries[doc] = entry
        return entries

    def record(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


# ------------------------------------------------------------------------------------- engine
class ChristmasSunbizLeadEngine:
    """Discovers and verifies Christmas / holiday lighting installers from Florida Sunbiz filings."""

    def __init__(
        self,
        config: ChristmasSunbizConfig | None = None,
        source: FloridaSunbizSource | None = None,
        finder: ChristmasWebsiteFinder | None = None,
        crawler: WebsiteCrawler | None = None,
        classifier: ChristmasLightClassifier | None = None,
        mail_checker: MailDomainChecker | None = None,
    ) -> None:
        self.config = config or ChristmasSunbizConfig()
        self.settings = get_settings()
        cfg = self.config
        self.output_csv = Path(
            cfg.output_csv or self.settings.output_dir / "christmas_holiday_lighting" / DEFAULT_OUTPUT_NAME
        )
        stem = self.output_csv.with_suffix("")
        self.review_csv = Path(f"{stem}_review.csv")
        self.state_path = Path(f"{stem}_state.jsonl")
        self.summary_path = Path(f"{stem}_run_summary.json")

        self.source = source or FloridaSunbizSource(timeout_seconds=self.settings.request_timeout_seconds)
        self.parser = SunbizRecordParser()
        self.finder = finder or ChristmasWebsiteFinder(
            timeout_seconds=cfg.timeout, search_interval=cfg.search_interval
        )
        self.crawler = crawler or WebsiteCrawler(
            timeout_seconds=cfg.timeout,
            max_subpages=10,
            config=dataclasses.replace(
                christmas_crawler_config(read_timeout=cfg.timeout), cache_namespace=CRAWL_CACHE_NAMESPACE
            ),
        )
        self.classifier = classifier or ChristmasLightClassifier()
        self.email_extractor = EmailExtractor(crawler=self.crawler)
        self.mail_checker = mail_checker or MailDomainChecker(enabled=cfg.check_mail_domains)

        self.lead_writer = IncrementalCsvWriter(self.output_csv, LEAD_COLUMNS)
        self.review_writer = IncrementalCsvWriter(self.review_csv, LEAD_COLUMNS)
        self.journal = ProcessingJournal(self.state_path)

        # Dedup state (filled from existing outputs at startup)
        self.lead_docs: set[str] = set()
        self.lead_names: set[str] = set()
        self.lead_domains: set[str] = set()
        self.review_docs: set[str] = set()
        self.journal_entries: dict[str, dict[str, Any]] = {}
        self.queued_docs: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._domain_claims: dict[str, str] = {}  # domain -> doc currently being finalized

    # ------------------------------------------------------------------ startup / resume
    def _prepare_outputs(self) -> None:
        self.lead_writer.ensure_header()
        self.review_writer.ensure_header()
        for row in self.lead_writer.read_rows():
            self._register_lead(row)
        for row in self.review_writer.read_rows():
            doc = (row.get("sunbiz_document_number") or "").strip().upper()
            if doc:
                self.review_docs.add(doc)
        self.journal_entries = self.journal.load()
        # Re-write leads that were verified but never reached the CSV (e.g. file locked at shutdown)
        for doc, entry in self.journal_entries.items():
            row = entry.get("row")
            if not row:
                continue
            if entry.get("status") == QUALIFIED and doc not in self.lead_docs:
                self.lead_writer.append(row)
                self._register_lead(row)
            elif entry.get("status") == REVIEW and doc not in self.review_docs:
                self.review_writer.append(row)
                self.review_docs.add(doc)

    def _register_lead(self, row: dict[str, str]) -> None:
        doc = (row.get("sunbiz_document_number") or "").strip().upper()
        if doc:
            self.lead_docs.add(doc)
        if row.get("company_name"):
            self.lead_names.add(company_key(row["company_name"]))
        if row.get("website"):
            self.lead_domains.add(extract_root_domain(row["website"]))

    def _should_skip(self, cand: SunbizCandidate) -> bool:
        doc = cand.document_number
        if doc in self.queued_docs or doc in self.lead_docs or company_key(cand.legal_name) in self.lead_names:
            return True
        if not self.config.resume:
            return False
        entry = self.journal_entries.get(doc)
        if not entry:
            return False
        if entry.get("status") in RETRYABLE_STATUSES:
            return int(entry.get("attempts", 1)) >= self.config.max_attempts
        return True

    # ------------------------------------------------------------------ Sunbiz scanning
    def _years(self) -> list[int]:
        if self.config.years:
            return sorted(set(self.config.years), reverse=True)
        root = self.settings.raw_data_dir / "sunbiz"
        if not root.exists():
            return []
        return sorted(
            (int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit() and any(p.glob("*c.txt"))),
            reverse=True,
        )

    def _iter_source_files(self) -> Iterator[tuple[str, Path | None, list[str] | None]]:
        """Yield (file name, local path, lines) per daily filing file, newest year first."""
        for year in self._years():
            if self.config.local_only:
                files = sorted(
                    p for p in (self.settings.raw_data_dir / "sunbiz" / str(year)).glob(f"{year}*c.txt")
                    if p.stat().st_size > 0
                )
                if self.config.max_files:
                    files = files[: self.config.max_files]
                for path in files:
                    yield path.name, path, None
            else:
                for name, _, _, lines in self.source.stream_files_for_year(year, max_files=self.config.max_files):
                    yield name, None, lines

    def _scan_lines(self, file_name: str, lines: Any) -> tuple[int, list[SunbizCandidate]]:
        found: list[SunbizCandidate] = []
        count = 0
        for line in lines:
            count += 1
            cand = candidate_from_line(
                line.rstrip("\r\n"), self.parser, self.config.scope, self.config.include_inactive, file_name
            )
            if cand:
                found.append(cand)
        return count, found

    def _scan_file(self, path: Path) -> tuple[int, list[SunbizCandidate]]:
        with open(path, encoding="latin-1", errors="replace") as f:
            return self._scan_lines(path.name, f)

    # ------------------------------------------------------------------ research
    def _build_row(
        self,
        cand: SunbizCandidate,
        website: str,
        discovery_source: str,
        identity: IdentityMatch,
        result: ChristmasLightClassificationResult,
        status_label: str,
        email: str = "",
        email_source: str = "",
        email_check: str = "",
    ) -> dict[str, str]:
        niches = result.qualified_niches or [
            n for v, n in (
                (result.christmas_service, "Christmas Light Installation"),
                (result.holiday_service, "Holiday Light Installation"),
                (result.permanent_service, "Permanent Lighting Installation"),
            ) if v == SERVICE_FIELD_YES
        ]
        return {
            "company_name": cand.legal_name,
            "email": email,
            "website": website,
            "formation_date": cand.formation_date,
            "sunbiz_document_number": cand.document_number,
            "city": cand.city,
            "state": cand.state,
            "service_category": "; ".join(niches),
            "verification_status": status_label,
            "identity_confidence": f"{identity.confidence:.2f}",
            "classification_confidence": f"{result.confidence:.2f}",
            "evidence_url": (result.evidence_urls or [website])[0],
            "evidence_summary": " ".join(result.reasoning.split())[:500],
            "discovery_source": discovery_source,
            "email_source": email_source,
            "email_check": email_check,
            "candidate_tier": str(cand.tier),
            "processed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    async def _select_email(self, crawl: CrawlResult, website: str) -> tuple[str, str, str]:
        """(email, page it was listed on, mail-domain check) - empty strings when nothing reliable."""
        candidates = await self.email_extractor.extract_emails(website, crawl_result=crawl)
        for cand in candidates:
            email = cand.email_address.lower().strip()
            if cand.initial_confidence < 0.7 or not EMAIL_REGEX.match(email):
                continue  # third-party domain (web designer, platform) or malformed
            check = await self.mail_checker.check(email.split("@", 1)[1])
            if check == MAIL_NO_HOST:
                continue
            page = next(
                (p.url for p in crawl.all_pages if email in {e.lower() for e in p.emails}),
                crawl.base_url or website,
            )
            return email, page, check
        return "", "", ""

    async def research(self, cand: SunbizCandidate) -> CandidateOutcome:
        """Find, verify, and classify the official website of one Sunbiz registration."""
        search = await self.finder.discover(
            company_name=cand.search_name,
            city=cand.city,
            state=cand.state,
            street_address=cand.street_address,
            zip_code=cand.zip_code,
        )
        if not search.candidates:
            if not search.search_responded:
                return CandidateOutcome(SEARCH_UNAVAILABLE, "No search provider answered (blocked/rate limited)")
            return CandidateOutcome(NO_WEBSITE, "No website candidates found")

        verified: tuple[str, str, CrawlResult, IdentityMatch] | None = None
        unreachable = 0
        mismatches: list[str] = []
        checked = 0
        for site in search.candidates:
            if checked >= self.config.max_candidate_sites:
                break
            if _is_excluded_domain(site.domain):
                continue
            checked += 1
            crawl = await self.crawler.crawl_domain(site.url)
            if not crawl.is_accessible:
                unreachable += 1
                continue
            identity = verify_company_identity(
                crawl, cand.search_name, cand.city, cand.zip_code, cand.street_address
            )
            if identity.is_match:
                verified = (site.url, site.discovery_source, crawl, identity)
                break
            mismatches.append(f"{site.domain}: {identity.reason}")

        if verified is None:
            if unreachable and not mismatches:
                return CandidateOutcome(WEBSITE_UNREACHABLE, f"{unreachable} candidate site(s) unreachable")
            detail = "; ".join(mismatches[:3]) or "only directory/retail domains found"
            return CandidateOutcome(WEBSITE_UNVERIFIED, f"No candidate matched the company - {detail}")

        url, discovery_source, crawl, identity = verified
        website = _site_root(crawl.homepage.url if crawl.homepage else crawl.base_url or url)

        result = self.classifier.evaluate_crawl(crawl, company_name=cand.search_name)
        if result.classification == "Unreachable":
            return CandidateOutcome(WEBSITE_UNREACHABLE, result.reasoning or "Website unreachable", website=website)

        if qualifies_for_niche(result) and result.confidence >= self.config.min_classification_confidence:
            email, email_source, email_check = await self._select_email(crawl, website)
            row = self._build_row(
                cand, website, discovery_source, identity, result, "Verified",
                email=email, email_source=email_source, email_check=email_check,
            )
            return CandidateOutcome(QUALIFIED, result.reasoning, row=row, website=website)

        if result.classification in ("Yes", "Uncertain"):
            email, email_source, email_check = await self._select_email(crawl, website)
            row = self._build_row(
                cand, website, discovery_source, identity, result, "Needs Review",
                email=email, email_source=email_source, email_check=email_check,
            )
            return CandidateOutcome(REVIEW, result.reasoning, row=row, website=website)

        return CandidateOutcome(NOT_QUALIFIED, result.reasoning or "No holiday lighting installation evidence", website=website)

    # ------------------------------------------------------------------ outcome handling
    async def _finalize(self, cand: SunbizCandidate, outcome: CandidateOutcome, summary: EngineSummary) -> None:
        async with self._write_lock:
            if outcome.status == QUALIFIED and outcome.row:
                domain = extract_root_domain(outcome.row["website"])
                if cand.document_number in self.lead_docs or domain in self.lead_domains:
                    outcome = CandidateOutcome(
                        DUPLICATE, f"Website {domain} already saved for another registration", website=outcome.website
                    )
                    console.print(f"  [dim]= {cand.legal_name}: duplicate of an existing lead ({domain})[/dim]")
                else:
                    written = self.lead_writer.append(outcome.row)
                    self._register_lead(outcome.row)
                    summary.new_leads += 1
                    if outcome.row.get("email"):
                        summary.leads_with_email += 1
                    console.print(
                        f"  [bold green]LEAD #{len(self.lead_docs):,}[/bold green] [green]{cand.legal_name}[/green]"
                        f" - {outcome.row['website']} - {outcome.row['email'] or '[dim](no public email)[/dim]'}"
                        f" [dim]({outcome.row['service_category']})[/dim]"
                        + ("" if written else " [yellow](pending: CSV locked)[/yellow]")
                    )
            elif outcome.status == REVIEW and outcome.row:
                if cand.document_number not in self.review_docs:
                    self.review_writer.append(outcome.row)
                    self.review_docs.add(cand.document_number)
                    summary.new_review += 1
                    console.print(f"  [yellow]REVIEW[/yellow] {cand.legal_name} - {outcome.row['website']}")
            else:
                console.print(f"  [dim]x {cand.legal_name}: {outcome.status} - {outcome.reason[:140]}[/dim]")

            previous = self.journal_entries.get(cand.document_number, {})
            attempts = int(previous.get("attempts", 0)) + 1 if previous.get("status") in RETRYABLE_STATUSES else 1
            entry = {
                "v": JOURNAL_VERSION,
                "doc": cand.document_number,
                "name": cand.legal_name,
                "status": outcome.status,
                "reason": outcome.reason[:500],
                "website": outcome.website,
                "attempts": attempts,
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if outcome.row:
                entry["row"] = outcome.row
            self.journal.record(entry)
            self.journal_entries[cand.document_number] = entry
            summary.outcomes[outcome.status] = summary.outcomes.get(outcome.status, 0) + 1

    async def _research_safely(self, cand: SunbizCandidate) -> CandidateOutcome:
        try:
            return await asyncio.wait_for(self.research(cand), timeout=self.config.candidate_timeout)
        except TimeoutError:
            return CandidateOutcome(TIMEOUT, f"Research exceeded {self.config.candidate_timeout:.0f}s")
        except Exception as exc:  # one bad site must never stop the run
            logger.warning(f"Research failed for {cand.legal_name} ({cand.document_number}): {exc}")
            return CandidateOutcome(ERROR, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ main loop
    async def run(self) -> EngineSummary:
        start = time.perf_counter()
        summary = EngineSummary(
            output_csv=str(self.output_csv.resolve()),
            review_csv=str(self.review_csv.resolve()),
            state_file=str(self.state_path.resolve()),
            started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self._prepare_outputs()  # creates the CSV + header before any research starts
        cfg = self.config
        console.print(
            f"[bold cyan]Christmas & holiday lighting lead engine[/bold cyan]  "
            f"scope={cfg.scope}  workers={cfg.concurrency}  "
            f"existing leads={len(self.lead_docs):,}  journal entries={len(self.journal_entries):,}\n"
            f"  Leads CSV : {self.output_csv.resolve()}\n"
            f"  Review CSV: {self.review_csv.resolve()}"
        )
        years = self._years()
        if not years:
            summary.stopped_reason = (
                "No Sunbiz data files found. Run with --download (and --year) to fetch filings from the portal."
            )
            console.print(f"[bold red]{summary.stopped_reason}[/bold red]")
            return self._finish(summary, start)
        console.print(f"  Sunbiz years: {', '.join(map(str, years))} ({'local cache' if cfg.local_only else 'portal + cache'})")

        queue: asyncio.PriorityQueue[tuple[int, int, SunbizCandidate | None]] = asyncio.PriorityQueue()
        stop = asyncio.Event()
        dispatched = 0
        seq = 0

        def halt(reason: str) -> None:
            """Stop taking new work; idle workers wake on top-priority sentinels, busy ones finish first."""
            nonlocal seq
            if stop.is_set():
                return
            stop.set()
            summary.stopped_reason = reason
            for _ in range(cfg.concurrency):
                seq += 1
                queue.put_nowait((-1, seq, None))

        async def produce() -> None:
            nonlocal seq
            iterator = self._iter_source_files()
            while not stop.is_set():
                item = await asyncio.to_thread(next, iterator, None)
                if item is None:
                    break
                name, path, lines = item
                if path is not None:
                    count, found = await asyncio.to_thread(self._scan_file, path)
                else:
                    count, found = await asyncio.to_thread(self._scan_lines, name, lines)
                summary.files_scanned += 1
                summary.records_scanned += count
                for cand in found:
                    if self._should_skip(cand):
                        if cand.document_number not in self.queued_docs:
                            summary.skipped_already_processed += 1
                        continue
                    self.queued_docs.add(cand.document_number)
                    summary.candidates_found += 1
                    seq += 1
                    await queue.put((cand.tier, seq, cand))
            for _ in range(cfg.concurrency):
                seq += 1
                await queue.put((99, seq, None))

        async def work() -> None:
            nonlocal dispatched
            while not stop.is_set():
                _, _, cand = await queue.get()
                if cand is None:
                    return
                if cfg.limit is not None and dispatched >= cfg.limit:
                    halt(f"--limit {cfg.limit} registrations researched")
                    return
                dispatched += 1
                summary.researched += 1
                outcome = await self._research_safely(cand)
                await self._finalize(cand, outcome, summary)
                if summary.researched % 25 == 0:
                    console.print(
                        f"[cyan]  progress: researched {summary.researched:,} | leads +{summary.new_leads} "
                        f"| review +{summary.new_review} | candidates queued {summary.candidates_found:,} "
                        f"| files {summary.files_scanned:,}[/cyan]"
                    )
                if cfg.max_leads is not None and summary.new_leads >= cfg.max_leads:
                    halt(f"--max-leads {cfg.max_leads} reached")
                    return

        producer = asyncio.create_task(produce())
        workers = [asyncio.create_task(work()) for _ in range(max(1, cfg.concurrency))]
        try:
            await asyncio.gather(*workers)
            if not summary.stopped_reason:
                summary.stopped_reason = "Sunbiz candidates exhausted"
        finally:
            stop.set()
            producer.cancel()
            for w in workers:
                w.cancel()
            await asyncio.gather(producer, *workers, return_exceptions=True)
            if hasattr(self.crawler, "aclose"):
                await self.crawler.aclose()
            self._finish(summary, start)
        return summary

    def _finish(self, summary: EngineSummary, start: float) -> EngineSummary:
        self.lead_writer.flush_pending()
        self.review_writer.flush_pending()
        summary.pending_unwritten_rows = len(self.lead_writer.pending) + len(self.review_writer.pending)
        summary.total_leads_in_csv = len(self.lead_docs)
        summary.duration_seconds = round(time.perf_counter() - start, 1)
        summary.finished_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with open(self.summary_path, "w", encoding="utf-8") as f:
                json.dump(dataclasses.asdict(summary), f, indent=2)
        except OSError as exc:
            logger.warning(f"Could not write run summary: {exc}")
        return summary


def print_engine_summary(summary: EngineSummary) -> None:
    outcomes = Counter(summary.outcomes)
    console.print(
        f"\n[bold green]Run finished[/bold green] ({summary.stopped_reason}) in {summary.duration_seconds:,.0f}s\n"
        f"  Sunbiz records scanned : {summary.records_scanned:,} in {summary.files_scanned:,} files\n"
        f"  Candidates researched  : {summary.researched:,} "
        f"(skipped as already processed: {summary.skipped_already_processed:,})\n"
        f"  New qualified leads    : {summary.new_leads:,} ({summary.leads_with_email:,} with email)\n"
        f"  New review rows        : {summary.new_review:,}\n"
        f"  Total leads in CSV     : {summary.total_leads_in_csv:,}\n"
        f"  Outcomes               : {dict(outcomes.most_common())}\n"
        f"  Leads CSV  : {summary.output_csv}\n"
        f"  Review CSV : {summary.review_csv}"
    )
    if summary.pending_unwritten_rows:
        console.print(
            f"[bold yellow]  {summary.pending_unwritten_rows} row(s) could not be written because the CSV is locked. "
            "They are saved in the state journal and will be written on the next run.[/bold yellow]"
        )
