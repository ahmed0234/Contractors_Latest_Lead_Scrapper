"""End-to-end production contractor lead-generation pipeline engine."""

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from contractor_scraper.acquisition.sunbiz import FloridaSunbizSource
from contractor_scraper.classification.classifier import get_classifier
from contractor_scraper.config.settings import get_settings
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
from contractor_scraper.parsing.sunbiz_parser import SunbizRecordParser
from contractor_scraper.storage.database import get_db_session, init_db
from contractor_scraper.storage.repository import LeadRepository

logger = get_logger("lead_engine")


@dataclass
class EngineRunTelemetry:
    """Detailed telemetry and execution metrics for a lead-generation run."""

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
    rejected_records: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    output_csv_path: Path | None = None
    production_csv_path: Path | None = None
    missing_contact_csv_path: Path | None = None

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

    async def _enrich_candidate(
        self,
        company: NormalizedCompanyRecord,
        category_name: str,
        semaphore: asyncio.Semaphore,
        run_id: str,
        telemetry: EngineRunTelemetry,
    ) -> FinalLead | None:
        """Asynchronously enrich an individual classified company through website and email verification."""
        async with semaphore:
            t0 = time.perf_counter()
            company_id = company.source_record_id
            name = company.normalized_name
            city = company.city
            state = company.state
            raw_meta = company.metadata or {}

            # Extract Florida Sunbiz filing metadata
            raw_file_date = raw_meta.get("file_date", "")
            formation_date = ""
            if len(raw_file_date) == 8 and raw_file_date.isdigit():
                formation_date = f"{raw_file_date[4:]}-{raw_file_date[:2]}-{raw_file_date[2:4]}"
            elif raw_file_date:
                formation_date = str(raw_file_date)

            status_code = raw_meta.get("status", "Active")
            company_status = "Active" if status_code == "A" else status_code
            street_address = raw_meta.get("address1", "")
            zip_code = raw_meta.get("zip", "")
            industry_name = category_name.replace("_", " ").replace("-", " ").title()

            # 1. Website Discovery
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
                lead = FinalLead(
                    company_name=name,
                    website="",
                    email="",
                    city=city,
                    state=state,
                    formation_date=formation_date,
                    document_number=company_id,
                    company_status=company_status,
                    street_address=street_address,
                    zip_code=zip_code,
                    industry=industry_name,
                    confidence_score=0.5,
                )
                if self.deduplicator.is_duplicate(lead):
                    telemetry.record_rejection("Duplicate lead consolidated")
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
                    self._record_attempt(
                        run_id,
                        company_id,
                        PipelineStage.WEBSITE_VERIFICATION,
                        AttemptStatus.SUCCESS,
                        None,
                        (time.perf_counter() - t_verify) * 1000,
                    )
                    break

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
                lead = FinalLead(
                    company_name=name,
                    website="",
                    email="",
                    city=city,
                    state=state,
                    formation_date=formation_date,
                    document_number=company_id,
                    company_status=company_status,
                    street_address=street_address,
                    zip_code=zip_code,
                    industry=industry_name,
                    confidence_score=0.5,
                )
                if self.deduplicator.is_duplicate(lead):
                    telemetry.record_rejection("Duplicate lead consolidated")
                    return None
                self.deduplicator.register(lead)
                return lead

            # 3. Public Email Collection (without SMTP or DNS deliverability testing)
            t_email = time.perf_counter()
            crawl_data = await self.crawler.crawl_domain(verified_site.url)
            email_candidates = await self.email_extractor.extract_emails(
                verified_site.url, crawl_result=crawl_data
            )

            found_email = ""
            if email_candidates:
                telemetry.emails_discovered += len(email_candidates)
                # Take highest-scoring public business email directly
                found_email = email_candidates[0].email_address.lower()
                self._record_attempt(
                    run_id,
                    company_id,
                    PipelineStage.EMAIL_EXTRACTION,
                    AttemptStatus.SUCCESS,
                    None,
                    (time.perf_counter() - t_email) * 1000,
                )
            else:
                self._record_attempt(
                    run_id,
                    company_id,
                    PipelineStage.EMAIL_EXTRACTION,
                    AttemptStatus.SUCCESS,
                    "No email found on website",
                    (time.perf_counter() - t_email) * 1000,
                )

            # 4. Build and validate Final Lead (clean practical lead record)
            try:
                lead = FinalLead(
                    company_name=name,
                    website=verified_site.url,
                    email=found_email,
                    city=city,
                    state=state,
                    formation_date=formation_date,
                    document_number=company_id,
                    company_status=company_status,
                    street_address=street_address,
                    zip_code=zip_code,
                    industry=industry_name,
                    confidence_score=verified_result.confidence,
                )

                # Deduplication check
                if self.deduplicator.is_duplicate(lead):
                    telemetry.record_rejection("Duplicate lead consolidated")
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
        """Helper to write enrichment attempt audit log."""
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

    async def run(
        self,
        year: int,
        category: str,
        limit: int = 10000,
        max_source_files: int | None = None,
    ) -> tuple[Path, EngineRunTelemetry]:
        """Execute the lead generation workflow for the requested year and category."""
        start_time = time.perf_counter()
        init_db()

        # 1. Resolve classifier
        classifier = get_classifier(category)
        category_enum = classifier.category
        category_slug = category_enum.value.lower().replace(" ", "_")

        telemetry = EngineRunTelemetry(
            year=year,
            category=category_enum.value,
            requested_limit=limit,
        )

        logger.info(
            f"Starting Contractor Lead Engine: Year={year}, Category={category_enum.value}, Target Limit={limit}"
        )

        # 2. Initialize Run in persistent database
        with get_db_session() as session:
            repo = LeadRepository(session)
            db_run = repo.create_run(category=category_enum, state="FL")
            run_id = db_run.id

        production_leads: list[FinalLead] = []
        missing_contact_leads: list[FinalLead] = []
        semaphore = asyncio.Semaphore(self.concurrency_limit)
        active_tasks: set[asyncio.Task] = set()

        def is_production_lead(lead: FinalLead) -> bool:
            """Check if lead has both verified website and public email."""
            return bool(lead.website and lead.website.strip()) and bool(
                lead.email and lead.email.strip()
            )

        def record_lead(lead: FinalLead) -> bool:
            """Register lead in appropriate list. Returns True if production limit is reached."""
            if is_production_lead(lead):
                if len(production_leads) < limit:
                    production_leads.append(lead)
                    telemetry.final_complete_leads += 1
                    logger.info(
                        f"Collected production lead #{len(production_leads)}: {lead.company_name} | {lead.city}, {lead.state} | {lead.website} | {lead.email}"
                    )
            else:
                missing_contact_leads.append(lead)
                telemetry.missing_contact_leads += 1
                logger.info(
                    f"Collected lead missing contact #{len(missing_contact_leads)}: {lead.company_name} | {lead.city}, {lead.state}"
                )
            return len(production_leads) >= limit

        # 3. Stream raw Sunbiz corporate filings
        try:
            raw_lines_stream = self.sunbiz_source.stream_raw_records_for_year(
                year=year,
                max_files=max_source_files,
            )

            for raw_line in raw_lines_stream:
                telemetry.raw_records_examined += 1

                # Parse Sunbiz line
                parsed = self.parser.parse_line(raw_line)
                if not parsed:
                    continue

                # Filter by active status
                if not parsed.raw_metadata.get("is_active", True):
                    continue

                # Normalize company
                normalized = self.normalizer.normalize(parsed)
                if not normalized:
                    continue

                # Classify company trade
                c_res = classifier.evaluate(
                    normalized.normalized_name, metadata=parsed.raw_metadata
                )
                if not c_res.is_match:
                    continue

                telemetry.category_candidates += 1

                # Persist company candidate into database
                with get_db_session() as session:
                    repo = LeadRepository(session)
                    fp = generate_company_fingerprint(
                        normalized.normalized_name, normalized.city, normalized.state
                    )
                    repo.upsert_company(
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

                # Spawn async enrichment task
                task = asyncio.create_task(
                    self._enrich_candidate(
                        company=normalized,
                        category_name=category_slug,
                        semaphore=semaphore,
                        run_id=run_id,
                        telemetry=telemetry,
                    )
                )
                active_tasks.add(task)

                # Process completed tasks in background
                if len(active_tasks) >= self.concurrency_limit * 2:
                    done, active_tasks = await asyncio.wait(
                        active_tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in done:
                        lead = await t
                        if lead and record_lead(lead):
                            break

                if len(production_leads) >= limit:
                    logger.info(
                        f"Reached requested production lead limit ({limit}). Stopping ingestion."
                    )
                    break

            # Await any remaining tasks
            if active_tasks:
                done, _ = await asyncio.wait(active_tasks)
                for t in done:
                    lead = await t
                    if lead:
                        record_lead(lead)

        except Exception as exc:
            logger.error(f"Error during lead generation pipeline: {exc}", exc_info=True)
            with get_db_session() as session:
                repo = LeadRepository(session)
                repo.complete_run(run_id=run_id, status=RunStatus.FAILED, error_summary=str(exc))
            raise

        telemetry.duration_seconds = round(time.perf_counter() - start_time, 2)

        # 4. Export leads into TWO separate CSV files:
        # File 1: Production-ready leads (complete with website and email)
        prod_canonical_path = self.output_dir / "production_leads.csv"
        prod_category_path = self.output_dir / f"{category_slug}_{year}_production_leads.csv"
        prod_legacy_path = self.output_dir / f"{category_slug}_{year}.csv"

        self.exporter.export_to_csv(production_leads, prod_canonical_path)
        self.exporter.export_to_csv(production_leads, prod_category_path)
        self.exporter.export_to_csv(production_leads, prod_legacy_path)

        # File 2: Leads missing email or website (valid category match, but email and/or website missing)
        missing_canonical_path = self.output_dir / "leads_missing_contact.csv"
        missing_category_path = (
            self.output_dir / f"{category_slug}_{year}_leads_missing_contact.csv"
        )

        self.exporter.export_to_csv(missing_contact_leads, missing_canonical_path)
        self.exporter.export_to_csv(missing_contact_leads, missing_category_path)

        telemetry.output_csv_path = prod_legacy_path
        telemetry.production_csv_path = prod_canonical_path
        telemetry.missing_contact_csv_path = missing_canonical_path

        # 5. Complete run in database
        with get_db_session() as session:
            repo = LeadRepository(session)
            repo.update_run_progress(
                run_id=run_id,
                processed_delta=telemetry.category_candidates,
                success_delta=len(production_leads),
                failed_delta=telemetry.rejected_records,
            )
            repo.complete_run(run_id=run_id, status=RunStatus.COMPLETED)

        logger.info(
            f"Finished run {run_id}. Outputs: {prod_canonical_path} ({len(production_leads)} production leads), "
            f"{missing_canonical_path} ({len(missing_contact_leads)} leads missing contact) in {telemetry.duration_seconds}s"
        )
        return prod_legacy_path, telemetry
