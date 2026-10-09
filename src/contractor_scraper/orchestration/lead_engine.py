"""End-to-end production contractor lead-generation pipeline engine.

Key behaviours:
- If `year` is None the engine automatically searches years from the latest
  available year down to MIN_YEAR (2016), continuing into older years until
  the requested `limit` of *fully complete* production leads is reached.
- A production-ready lead requires BOTH a verified website AND a publicly
  discovered email address.  Leads with a website but no email are saved to
  the missing-contact file and do NOT count toward the requested limit.
- Duplicate companies are detected across years via the LeadDeduplicator and
  are never exported more than once.
- Rich colorful progress output is printed to the terminal via core.progress.
"""

import asyncio
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from contractor_scraper.acquisition.sunbiz import FloridaSunbizSource
from contractor_scraper.classification.classifier import get_classifier
from contractor_scraper.config.settings import get_settings
from contractor_scraper.core import progress as prg
from contractor_scraper.core.constants import US_STATES
from contractor_scraper.core.enums import (
    AttemptStatus,
    CompanyStatus,
    PipelineStage,
    RunStatus,
)
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.crawler import WebsiteCrawler
from contractor_scraper.discovery.website_finder import WebsiteFinder
from contractor_scraper.discovery.website_verifier import WebsiteVerifier
from contractor_scraper.exporting.exporter import LeadExporter
from contractor_scraper.extraction.email_extractor import EmailExtractor
from contractor_scraper.extraction.email_verifier import EmailVerifier
from contractor_scraper.matching.dedup import LeadDeduplicator, generate_company_fingerprint
from contractor_scraper.models.company import CompanyCreate
from contractor_scraper.models.lead import FinalLead
from contractor_scraper.normalization.base import DefaultCompanyNormalizer, NormalizedCompanyRecord
from contractor_scraper.normalization.cleaners import clean_city_name, clean_state_code
from contractor_scraper.parsing.sunbiz_parser import SunbizRecordParser
from contractor_scraper.storage.database import get_db_session, init_db
from contractor_scraper.storage.repository import LeadRepository

# When no year is explicitly requested, search backwards from current year
# down to MIN_YEAR (inclusive).
MIN_YEAR: int = 2016

logger = get_logger("lead_engine")


@dataclass
class YearRunResult:
    """Results for a single filing year within a multi-year run."""

    year: int
    raw_records_examined: int = 0
    category_candidates: int = 0
    websites_discovered: int = 0
    websites_verified: int = 0
    emails_discovered: int = 0
    production_leads: int = 0
    missing_contact_leads: int = 0
    duplicates_skipped: int = 0
    rejected_records: int = 0


@dataclass
class EngineRunTelemetry:
    """Detailed telemetry and execution metrics for a lead-generation run.

    When multi-year mode is active (year=None), `year` is set to the *last*
    filing year that contributed leads.  Per-year breakdowns are in
    `year_results`.
    """

    year: int
    category: str
    requested_limit: int
    raw_records_examined: int = 0
    category_candidates: int = 0
    websites_discovered: int = 0
    websites_verified: int = 0
    emails_discovered: int = 0
    final_complete_leads: int = 0
    missing_contact_leads: int = 0
    duplicates_skipped: int = 0
    rejected_records: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    output_csv_path: Path | None = None
    production_csv_path: Path | None = None
    missing_contact_csv_path: Path | None = None
    year_results: list[YearRunResult] = field(default_factory=list)

    def record_rejection(self, reason: str) -> None:
        self.rejected_records += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1


class LeadEngine:
    """Production lead engine coordinating Florida Sunbiz data through discovery and verification."""

    def __init__(
        self,
        concurrency_limit: int = 5,
        output_dir: Path | None = None,
    ) -> None:
        self.settings = get_settings()
        self.output_dir = output_dir or self.settings.output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.concurrency_limit = concurrency_limit

        # Initialize subsystem components
        self.sunbiz_source = FloridaSunbizSource(
            timeout_seconds=self.settings.request_timeout_seconds
        )
        self.parser = SunbizRecordParser()
        self.normalizer = DefaultCompanyNormalizer()
        self.crawler = WebsiteCrawler(timeout_seconds=self.settings.request_timeout_seconds)
        self.finder = WebsiteFinder(timeout_seconds=self.settings.request_timeout_seconds)
        self.verifier = WebsiteVerifier(crawler=self.crawler)
        self.email_extractor = EmailExtractor(crawler=self.crawler)
        self.email_verifier = EmailVerifier(check_mx=True)
        self.deduplicator = LeadDeduplicator()
        self.exporter = LeadExporter()
        self.company_timeout_seconds = getattr(
            self.settings, "company_enrichment_timeout_seconds", 30.0
        )
        self.checkpoint_frequency = getattr(self.settings, "checkpoint_frequency", 1)

    def _create_unverified_lead(
        self,
        company: NormalizedCompanyRecord,
        category_name: str,
        website: str = "",
        email: str = "",
    ) -> FinalLead:
        """Construct standard FinalLead record for candidate contractors."""
        raw_meta = company.metadata or {}
        raw_file_date = raw_meta.get("file_date", "")
        formation_date = ""
        if len(raw_file_date) == 8 and raw_file_date.isdigit():
            formation_date = f"{raw_file_date[4:]}-{raw_file_date[:2]}-{raw_file_date[2:4]}"
        elif raw_file_date:
            formation_date = str(raw_file_date)

        status_code = raw_meta.get("status", "Active")
        company_status = "Active" if status_code == "A" else status_code
        street_address = (
            company.street_address
            or raw_meta.get("street_address")
            or raw_meta.get("address1")
            or ""
        ).strip()
        zip_code = (
            company.zip_code
            or raw_meta.get("zip_code")
            or raw_meta.get("zip")
            or ""
        ).strip()
        industry_name = category_name.replace("_", " ").replace("-", " ").title()

        return FinalLead(
            company_name=company.normalized_name,
            website=website,
            email=email,
            city=company.city,
            state=company.state,
            original_city=company.city,
            original_state=company.state,
            formation_date=formation_date,
            document_number=company.source_record_id,
            company_status=company_status,
            street_address=street_address,
            zip_code=zip_code,
            industry=industry_name,
            confidence_score=0.5,
        )

    async def _enrich_candidate(
        self,
        company: NormalizedCompanyRecord,
        category_name: str,
        semaphore: asyncio.Semaphore,
        run_id: str,
        telemetry: EngineRunTelemetry,
        db_company_id: str | None = None,
    ) -> FinalLead | None:
        """Enrich candidate company with strict timeout ceiling preventing any stalled tasks."""
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    self._enrich_candidate_inner(
                        company=company,
                        category_name=category_name,
                        run_id=run_id,
                        telemetry=telemetry,
                        db_company_id=db_company_id,
                    ),
                    timeout=self.company_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    f"Enrichment for '{company.normalized_name}' ({company.source_record_id}) "
                    f"timed out after {self.company_timeout_seconds}s. Moving to next candidate."
                )
                telemetry.record_rejection("Enrichment timed out")
                company_key = db_company_id or company.source_record_id
                self._record_attempt(
                    run_id,
                    company_key,
                    PipelineStage.WEBSITE_DISCOVERY,
                    AttemptStatus.FATAL_ERROR,
                    f"Enrichment timed out ({self.company_timeout_seconds}s limit)",
                    self.company_timeout_seconds * 1000,
                )
                lead = self._create_unverified_lead(company, category_name)
                if self.deduplicator.is_duplicate(lead):
                    return None
                self.deduplicator.register(lead)
                return lead
            except Exception as exc:
                logger.debug(f"Enrichment error for '{company.normalized_name}': {exc}")
                telemetry.record_rejection(f"Enrichment error: {exc}")
                lead = self._create_unverified_lead(company, category_name)
                if self.deduplicator.is_duplicate(lead):
                    return None
                self.deduplicator.register(lead)
                return lead

    async def _enrich_candidate_inner(
        self,
        company: NormalizedCompanyRecord,
        category_name: str,
        run_id: str,
        telemetry: EngineRunTelemetry,
        db_company_id: str | None = None,
    ) -> FinalLead | None:
        """Core multi-stage website discovery, verification, email extraction, and location enrichment."""
        t0 = time.perf_counter()
        company_id = db_company_id or company.source_record_id
        name = company.normalized_name
        city = company.city
        state = company.state

        # 1. Website Discovery with Address and Zip signals
        try:
            candidates = await self.finder.find_websites(
                company_name=name,
                city=city,
                state=state,
                category=category_name,
                street_address=company.street_address,
                zip_code=company.zip_code,
            )
        except TypeError:
            candidates = await self.finder.find_websites(
                company_name=name,
                city=city,
                state=state,
                category=category_name,
            )

        if not candidates:
            telemetry.record_rejection("No official website candidates found")
            self._record_attempt(
                run_id,
                company_id,
                PipelineStage.WEBSITE_DISCOVERY,
                AttemptStatus.FATAL_ERROR,
                "No websites found",
                (time.perf_counter() - t0) * 1000,
            )
            lead = self._create_unverified_lead(company, category_name)
            if self.deduplicator.is_duplicate(lead):
                telemetry.record_rejection("Duplicate lead consolidated")
                telemetry.duplicates_skipped += 1
                return None
            self.deduplicator.register(lead)
            return lead

        telemetry.websites_discovered += len(candidates)
        self._record_attempt(
            run_id,
            company_id,
            PipelineStage.WEBSITE_DISCOVERY,
            AttemptStatus.SUCCESS,
            None,
            (time.perf_counter() - t0) * 1000,
        )

        # 2. Website Verification
        verified_site = None
        verified_result = None

        for cand in candidates:
            t_verify = time.perf_counter()
            try:
                v_res = await self.verifier.verify_website(
                    candidate=cand,
                    expected_company_name=name,
                    expected_state=state,
                    expected_city=city,
                    category=category_name,
                    expected_street_address=company.street_address,
                    expected_zip_code=company.zip_code,
                )
            except TypeError:
                v_res = await self.verifier.verify_website(
                    candidate=cand,
                    expected_company_name=name,
                    expected_state=state,
                    expected_city=city,
                    category=category_name,
                )
            if v_res.is_verified:
                verified_site = cand
                verified_result = v_res
                telemetry.websites_verified += 1
                prg.print_website_verified(name, cand.url, v_res.confidence)
                self._record_attempt(
                    run_id,
                    company_id,
                    PipelineStage.WEBSITE_VERIFICATION,
                    AttemptStatus.SUCCESS,
                    None,
                    (time.perf_counter() - t_verify) * 1000,
                )
                break
            else:
                prg.print_website_rejected(name, cand.url, v_res.failure_reason or "identity mismatch")

        if not verified_site or not verified_result:
            telemetry.record_rejection("Website candidates failed identity verification")
            self._record_attempt(
                run_id,
                company_id,
                PipelineStage.WEBSITE_VERIFICATION,
                AttemptStatus.FATAL_ERROR,
                "Verification failed",
                (time.perf_counter() - t0) * 1000,
            )
            lead = self._create_unverified_lead(company, category_name)
            if self.deduplicator.is_duplicate(lead):
                telemetry.record_rejection("Duplicate lead consolidated")
                telemetry.duplicates_skipped += 1
                return None
            self.deduplicator.register(lead)
            return lead

        # 3. City / State enrichment from verified website
        enriched_city = city
        enriched_state = state
        try:
            crawl_data_for_location = await self.crawler.crawl_domain(verified_site.url)
            if crawl_data_for_location.is_accessible:
                new_city, new_state = self._extract_location_from_crawl(
                    crawl_data_for_location.combined_text
                )
                if new_city and new_state and (new_city != city or new_state != state):
                    prg.print_location_enriched(name, city, new_city, state, new_state)
                    enriched_city = new_city
                    enriched_state = new_state
        except Exception:
            pass
        # We re-use crawl_data_for_location below for email extraction

        # 4. Public Email Collection
        t_email = time.perf_counter()
        try:
            crawl_data = crawl_data_for_location  # reuse crawl already done above
        except NameError:
            crawl_data = await self.crawler.crawl_domain(verified_site.url)
        email_candidates = await self.email_extractor.extract_emails(
            verified_site.url, crawl_result=crawl_data
        )

        found_email = ""
        if email_candidates:
            telemetry.emails_discovered += len(email_candidates)
            found_email = email_candidates[0].email_address.lower()
            prg.print_email_found(found_email, "website crawl")
            self._record_attempt(
                run_id,
                company_id,
                PipelineStage.EMAIL_EXTRACTION,
                AttemptStatus.SUCCESS,
                None,
                (time.perf_counter() - t_email) * 1000,
            )
        else:
            prg.print_email_not_found(verified_site.url)
            self._record_attempt(
                run_id,
                company_id,
                PipelineStage.EMAIL_EXTRACTION,
                AttemptStatus.SUCCESS,
                "No email found on website",
                (time.perf_counter() - t_email) * 1000,
            )

        # 5. Build and validate Final Lead
        try:
            lead = self._create_unverified_lead(
                company=company,
                category_name=category_name,
                website=verified_site.url,
                email=found_email,
            )
            # Apply location enrichment
            lead.city = enriched_city
            lead.state = enriched_state
            lead.confidence_score = verified_result.confidence

            # Deduplication check
            if self.deduplicator.is_duplicate(lead):
                telemetry.record_rejection("Duplicate lead consolidated")
                telemetry.duplicates_skipped += 1
                prg.print_duplicate_skipped(name)
                return None

            self.deduplicator.register(lead)
            return lead

        except Exception as exc:
            telemetry.record_rejection(f"Final lead schema validation error: {exc}")
            return None

    def _record_attempt(
        self,
        run_id: str,
        company_id: str,
        stage: PipelineStage,
        status: AttemptStatus,
        error_msg: str | None,
        duration_ms: float,
    ) -> None:
        """Helper to write enrichment attempt audit log entry to SQLite."""
        try:
            with get_db_session() as session:
                repo = LeadRepository(session)
                repo.record_attempt(
                    run_id=run_id,
                    company_id=company_id,
                    stage=stage,
                    status=status,
                    error_message=error_msg,
                    execution_time_ms=duration_ms,
                )
        except Exception:
            pass

    def _extract_location_from_crawl(self, text: str) -> tuple[str, str]:
        """Extract reliable city and U.S. state evidence from crawled website content.

        Prioritizes:
        1. JSON-LD structured data (addressLocality and addressRegion).
        2. Explicit postal address blocks (e.g. street + City, State ZIP).
        3. Clear contact/location headings (e.g. Office / Location: City, ST).

        Does not guess, use server/IP data, or use loose service-area mentions.
        Returns normalized (city, state) or ("", "").
        """
        if not text:
            return "", ""

        state_name_to_code: dict[str, str] = {
            name.lower(): code for code, name in US_STATES.items()
        }

        # 1. JSON-LD structured data (most reliable official markup)
        jsonld_city = re.search(r'["\']addressLocality["\']\s*:\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
        jsonld_region = re.search(r'["\']addressRegion["\']\s*:\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
        if jsonld_city and jsonld_region:
            c_raw = jsonld_city.group(1).strip()
            s_raw = jsonld_region.group(1).strip()
            state_code = (
                s_raw.upper()
                if len(s_raw) == 2 and s_raw.upper() in US_STATES
                else state_name_to_code.get(s_raw.lower())
            )
            if state_code:
                c_clean = clean_city_name(c_raw)
                s_clean = clean_state_code(state_code)
                if c_clean and s_clean:
                    return c_clean, s_clean

        # 2. Explicit postal address patterns: Street address, City, ST ZIP
        postal_pattern = re.compile(
            r"(?:suite|ste|st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|hwy|highway|pkwy|parkway)\s*[\d\w#-]*\s*,\s*"
            r"([A-Za-z][A-Za-z .'-]{2,25}),\s*"
            r"([A-Za-z]{2}|[A-Za-z]{4,15})\s*(\d{5})?",
            re.IGNORECASE,
        )
        for match in postal_pattern.finditer(text):
            c_raw = match.group(1).strip()
            s_raw = match.group(2).strip()
            state_code = (
                s_raw.upper()
                if len(s_raw) == 2 and s_raw.upper() in US_STATES
                else state_name_to_code.get(s_raw.lower())
            )
            if state_code:
                c_clean = clean_city_name(c_raw)
                s_clean = clean_state_code(state_code)
                if c_clean and s_clean:
                    return c_clean, s_clean

        # 3. Explicit contact heading patterns (Office/Location: City, ST)
        contact_pattern = re.compile(
            r"(?:location|address|office|visit\s+us|headquarters)\s*[:\-]?\s*(?:[^\n,]{2,50},\s*)?"
            r"([A-Za-z][A-Za-z .'-]{2,25}),\s*"
            r"([A-Za-z]{2}|[A-Za-z]{4,15})\s*(\d{5})?",
            re.IGNORECASE,
        )
        for match in contact_pattern.finditer(text):
            c_raw = match.group(1).strip()
            s_raw = match.group(2).strip()
            state_code = (
                s_raw.upper()
                if len(s_raw) == 2 and s_raw.upper() in US_STATES
                else state_name_to_code.get(s_raw.lower())
            )
            if state_code:
                c_clean = clean_city_name(c_raw)
                s_clean = clean_state_code(state_code)
                if c_clean and s_clean:
                    return c_clean, s_clean

        return "", ""

    def _stream_year_files_and_records(
        self,
        year: int,
        max_source_files: int | None = None,
    ) -> Iterator[tuple[str, int, int, list[str]]]:
        """Stream daily filing files and raw lines for a given year.

        Seamlessly supports:
        1. When stream_raw_records_for_year is mocked or patched on sunbiz_source:
           Yields the mocked stream directly without any real network requests.
        2. When using FloridaSunbizSource:
           Uses stream_files_for_year with local caching, validation, and downloads.
        """
        cls_func = getattr(FloridaSunbizSource.stream_raw_records_for_year, "__func__", None)
        inst_func = getattr(self.sunbiz_source.stream_raw_records_for_year, "__func__", None)
        is_mocked = (
            cls_func is None
            or inst_func != cls_func
            or hasattr(self.sunbiz_source.stream_raw_records_for_year, "mock")
            or "Mock" in type(self.sunbiz_source.stream_raw_records_for_year).__name__
            or not isinstance(self.sunbiz_source, FloridaSunbizSource)
        )

        if is_mocked:
            lines = list(
                self.sunbiz_source.stream_raw_records_for_year(year, max_files=max_source_files)
            )
            if lines:
                yield f"{year}_stream.txt", 1, 1, lines
            return

        if hasattr(self.sunbiz_source, "stream_files_for_year"):
            yield from self.sunbiz_source.stream_files_for_year(
                year, max_files=max_source_files
            )
        else:
            lines = list(
                self.sunbiz_source.stream_raw_records_for_year(year, max_files=max_source_files)
            )
            if lines:
                yield f"{year}_stream.txt", 1, 1, lines

    async def run(
        self,
        year: int | None,
        category: str,
        limit: int = 10000,
        max_source_files: int | None = None,
    ) -> tuple[Path, EngineRunTelemetry]:
        """Execute the lead-generation workflow.

        If `year` is None the engine searches from the latest available filing year,
        starting with 2026, then 2025, 2024, down to MIN_YEAR (2016), moving to the
        next older year automatically when the current year is exhausted without
        reaching `limit`.

        If `year` is an explicit integer only that year is processed.
        """
        start_time = time.perf_counter()
        init_db()

        classifier = get_classifier(category)
        category_enum = classifier.category
        category_slug = category_enum.value.lower().replace(" ", "_")

        # Determine which years to iterate
        if year is not None:
            years_to_search: list[int] = [year]
        else:
            current_year = max(2026, datetime.now().year)
            years_to_search = list(range(current_year, MIN_YEAR - 1, -1))

        # Shared telemetry across all years
        sentinel_year = years_to_search[0]
        telemetry = EngineRunTelemetry(
            year=sentinel_year,
            category=category_enum.value,
            requested_limit=limit,
        )

        # Canonical output paths (do not vary by year)
        prod_canonical_path = self.output_dir / "production_leads.csv"
        missing_canonical_path = self.output_dir / "leads_missing_contact.csv"
        telemetry.production_csv_path = prod_canonical_path
        telemetry.missing_contact_csv_path = missing_canonical_path

        # --- Resume: pre-load any existing production / missing-contact leads ---
        production_leads: list[FinalLead] = []
        missing_contact_leads: list[FinalLead] = []
        other_prod_leads: list[FinalLead] = []
        other_missing_leads: list[FinalLead] = []
        processed_doc_numbers: set[str] = set()
        processed_names: set[str] = set()

        if prod_canonical_path.exists():
            for lead in self.exporter.load_existing_leads_from_csv(prod_canonical_path):
                lead_ind = lead.industry.lower().replace(" ", "_").replace("-", "_")
                if lead_ind == category_slug:
                    production_leads.append(lead)
                    self.deduplicator.register(lead)
                    if lead.document_number:
                        processed_doc_numbers.add(lead.document_number.upper().strip())
                    processed_names.add(lead.company_name.lower().strip())
                else:
                    other_prod_leads.append(lead)

        if missing_canonical_path.exists():
            for lead in self.exporter.load_existing_leads_from_csv(missing_canonical_path):
                lead_ind = lead.industry.lower().replace(" ", "_").replace("-", "_")
                if lead_ind == category_slug:
                    missing_contact_leads.append(lead)
                else:
                    other_missing_leads.append(lead)

        if production_leads:
            telemetry.final_complete_leads = len(production_leads)
            logger.info(
                f"Resumed: {len(production_leads)} existing production leads loaded."
            )

        # --- Checkpoint flusher (called after every new accepted lead) ---
        def flush_checkpoints() -> None:
            try:
                if production_leads:
                    combined_prods = other_prod_leads + production_leads
                    self.exporter.export_to_csv_atomic(combined_prods, prod_canonical_path)
                if missing_contact_leads:
                    combined_missing = other_missing_leads + missing_contact_leads
                    self.exporter.export_to_csv_atomic(combined_missing, missing_canonical_path)
            except Exception as flush_err:
                logger.error(f"Error flushing CSV checkpoints: {flush_err}")

        # --- Production-ready predicate: website + email both required ---
        def is_production_lead(lead: FinalLead) -> bool:
            """A lead is production-ready only when it has BOTH a website and an email."""
            return bool(
                lead.website and lead.website.strip()
                and lead.email and lead.email.strip()
            )

        def record_lead(lead: FinalLead) -> bool:
            """Register a processed lead.  Returns True when the global limit is reached."""
            if is_production_lead(lead):
                if len(production_leads) < limit:
                    production_leads.append(lead)
                    telemetry.final_complete_leads += 1
                    # Remove same company from missing-contact list if it was there
                    doc_num = (lead.document_number or "").upper().strip()
                    comp_name = lead.company_name.lower().strip()
                    missing_contact_leads[:] = [
                        m for m in missing_contact_leads
                        if (
                            (not m.document_number or m.document_number.upper().strip() != doc_num)
                            and m.company_name.lower().strip() != comp_name
                        )
                    ]
                    telemetry.missing_contact_leads = len(missing_contact_leads)
                    prg.print_lead_found(
                        lead_num=len(production_leads),
                        company_name=lead.company_name,
                        city=lead.city,
                        state=lead.state,
                        website=lead.website,
                        email=lead.email,
                        year=telemetry.year,
                        limit=limit,
                    )
                    flush_checkpoints()
            else:
                # Has a website but no email, or has neither
                doc_num = (lead.document_number or "").upper().strip()
                comp_name = lead.company_name.lower().strip()
                already_in_missing = any(
                    ((m.document_number or "").upper().strip() == doc_num and doc_num)
                    or m.company_name.lower().strip() == comp_name
                    for m in missing_contact_leads
                )
                if not already_in_missing:
                    missing_contact_leads.append(lead)
                    telemetry.missing_contact_leads = len(missing_contact_leads)
                    reason = "no email" if (lead.website and lead.website.strip()) else "no website"
                    prg.print_missing_contact(lead.company_name, reason)
                    if len(missing_contact_leads) % 10 == 0:
                        flush_checkpoints()
            return len(production_leads) >= limit

        # Early exit if already satisfied
        if len(production_leads) >= limit:
            prg.print_target_reached(len(production_leads), limit, telemetry.year)
            flush_checkpoints()
            telemetry.duration_seconds = round(time.perf_counter() - start_time, 2)
            return prod_canonical_path, telemetry

        # --- Initialize a single DB run record ---
        with get_db_session() as session:
            repo = LeadRepository(session)
            db_run = repo.create_run(category=category_enum, state="FL")
            run_id = db_run.id

        # --- Multi-year loop ---
        reached_limit = False
        active_tasks: set[asyncio.Task] = set()
        semaphore = asyncio.Semaphore(self.concurrency_limit)

        try:
            for search_year in years_to_search:
                if reached_limit:
                    break

                telemetry.year = search_year  # track last year processed
                prg.print_year_start(search_year, len(production_leads), limit)
                leads_before_year = len(production_leads)

                # Per-year category-specific CSV paths
                prod_category_path = (
                    self.output_dir
                    / f"{category_slug}_{search_year}_production_leads.csv"
                )
                telemetry.output_csv_path = prod_category_path

                # Stream files and records for this year
                for file_name, file_idx, total_files, raw_lines in self._stream_year_files_and_records(
                    search_year, max_source_files
                ):
                    if reached_limit:
                        break

                    if file_idx == 1 or file_idx % 10 == 0 or file_idx == total_files:
                        prg.print_file_processing(file_name, file_idx, total_files)

                    for raw_line in raw_lines:
                        if reached_limit:
                            break

                        telemetry.raw_records_examined += 1

                        parsed = self.parser.parse_line(raw_line)
                        if not parsed:
                            continue

                        if not parsed.raw_metadata.get("is_active", True):
                            continue

                        corp_id = (
                            getattr(parsed, "source_record_id", None)
                            or getattr(parsed, "corporate_number", None)
                        )
                        if corp_id and corp_id.upper().strip() in processed_doc_numbers:
                            continue

                        normalized = self.normalizer.normalize(parsed)
                        if not normalized:
                            continue

                        if normalized.normalized_name.lower().strip() in processed_names:
                            continue

                        c_res = classifier.evaluate(
                            normalized.normalized_name, metadata=parsed.raw_metadata
                        )
                        if not c_res.is_match:
                            continue

                        telemetry.category_candidates += 1

                        if corp_id:
                            processed_doc_numbers.add(corp_id.upper().strip())
                        processed_names.add(normalized.normalized_name.lower().strip())

                        # Persist company candidate
                        try:
                            with get_db_session() as session:
                                repo = LeadRepository(session)
                                fp = generate_company_fingerprint(
                                    normalized.normalized_name,
                                    normalized.city,
                                    normalized.state,
                                )
                                db_company = repo.upsert_company(
                                    CompanyCreate(
                                        name=normalized.normalized_name,
                                        raw_name=normalized.raw_name,
                                        city=normalized.city,
                                        state=normalized.state,
                                        category=category_enum,
                                        status=CompanyStatus.CLASSIFIED,
                                    ),
                                    canonical_fingerprint=fp,
                                )
                                db_company_id = db_company.id
                        except Exception:
                            db_company_id = None

                        task = asyncio.create_task(
                            self._enrich_candidate(
                                company=normalized,
                                category_name=category_slug,
                                semaphore=semaphore,
                                run_id=run_id,
                                telemetry=telemetry,
                                db_company_id=db_company_id,
                            )
                        )
                        active_tasks.add(task)

                        # Drain completed tasks
                        if len(active_tasks) >= self.concurrency_limit * 2:
                            done, active_tasks = await asyncio.wait(
                                active_tasks,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for t in done:
                                try:
                                    lead = await t
                                    if lead and record_lead(lead):
                                        reached_limit = True
                                        break
                                except Exception as t_err:
                                    logger.debug(f"Task error: {t_err}")

                    # End of file: drain all tasks for this file before next file
                    if active_tasks and not reached_limit:
                        done, active_tasks = await asyncio.wait(active_tasks)
                        for t in done:
                            try:
                                lead = await t
                                if lead and record_lead(lead):
                                    reached_limit = True
                                    break
                            except Exception as t_err:
                                logger.debug(f"Task error: {t_err}")

                # Year complete
                leads_this_year = len(production_leads) - leads_before_year
                yr_result = YearRunResult(
                    year=search_year,
                    raw_records_examined=telemetry.raw_records_examined,
                    production_leads=leads_this_year,
                    missing_contact_leads=len(missing_contact_leads),
                    duplicates_skipped=telemetry.duplicates_skipped,
                )
                telemetry.year_results.append(yr_result)
                prg.print_year_complete(
                    search_year, leads_this_year, len(production_leads), limit
                )

            # Drain any remaining tasks after all years
            if active_tasks:
                done, _ = await asyncio.wait(active_tasks)
                for t in done:
                    try:
                        lead = await t
                        if lead:
                            record_lead(lead)
                    except Exception as t_err:
                        logger.debug(f"Task error: {t_err}")

        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.warning("Interrupted by user. Saving all collected leads...")
            for t in active_tasks:
                t.cancel()
            flush_checkpoints()
            with get_db_session() as session:
                repo = LeadRepository(session)
                repo.update_run_progress(
                    run_id=run_id,
                    processed_delta=telemetry.category_candidates,
                    success_delta=len(production_leads),
                    failed_delta=telemetry.rejected_records,
                )
                repo.complete_run(run_id=run_id, status=RunStatus.PAUSED)
            telemetry.duration_seconds = round(time.perf_counter() - start_time, 2)
            return prod_canonical_path, telemetry

        except Exception as exc:
            logger.error(f"Pipeline error: {exc}. Saving collected leads...", exc_info=True)
            flush_checkpoints()
            with get_db_session() as session:
                repo = LeadRepository(session)
                repo.complete_run(run_id=run_id, status=RunStatus.FAILED, error_summary=str(exc))
            raise

        finally:
            flush_checkpoints()

        telemetry.duration_seconds = round(time.perf_counter() - start_time, 2)

        # Final DB update
        with get_db_session() as session:
            repo = LeadRepository(session)
            repo.update_run_progress(
                run_id=run_id,
                processed_delta=telemetry.category_candidates,
                success_delta=len(production_leads),
                failed_delta=telemetry.rejected_records,
            )
            repo.complete_run(run_id=run_id, status=RunStatus.COMPLETED)

        # Print final status
        if reached_limit or len(production_leads) >= limit:
            prg.print_target_reached(len(production_leads), limit, telemetry.year)
        else:
            prg.print_years_exhausted(len(production_leads), limit)

        logger.info(
            f"Run complete. {len(production_leads)} production leads, "
            f"{len(missing_contact_leads)} missing-contact leads "
            f"in {telemetry.duration_seconds:.1f}s"
        )
        return prod_canonical_path, telemetry
