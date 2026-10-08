"""Repository layer providing clean CRUD operations, checkpointing, and restartable querying."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from contractor_scraper.core.constants import MIN_LEAD_CONFIDENCE_THRESHOLD
from contractor_scraper.core.enums import (
    AttemptStatus,
    CompanyStatus,
    ContractorCategory,
    PipelineStage,
    RunStatus,
)
from contractor_scraper.models.company import CompanyCreate
from contractor_scraper.models.lead import FinalLead
from contractor_scraper.storage.models import (
    CompanyModel,
    EnrichmentAttemptModel,
    ProcessingRunModel,
)


class LeadRepository:
    """Repository handling persistence, stage checkpointing, and restartable execution state."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # --- Pipeline Runs & Checkpointing ---

    def create_run(
        self,
        category: ContractorCategory,
        state: str | None = None,
        total_records: int = 0,
    ) -> ProcessingRunModel:
        """Create a new batch execution run record."""
        run = ProcessingRunModel(
            id=str(uuid4()),
            category=category.value if isinstance(category, ContractorCategory) else category,
            state=state.upper() if state else None,
            status=RunStatus.RUNNING.value,
            total_records=total_records,
            processed_records=0,
            successful_records=0,
            failed_records=0,
            skipped_records=0,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def update_run_progress(
        self,
        run_id: str,
        processed_delta: int = 0,
        success_delta: int = 0,
        failed_delta: int = 0,
        skipped_delta: int = 0,
    ) -> None:
        """Checkpoint progress metrics incrementally."""
        run = self.session.get(ProcessingRunModel, run_id)
        if run:
            run.processed_records += processed_delta
            run.successful_records += success_delta
            run.failed_records += failed_delta
            run.skipped_records += skipped_delta
            self.session.flush()

    def complete_run(
        self,
        run_id: str,
        status: RunStatus = RunStatus.COMPLETED,
        error_summary: str | None = None,
    ) -> None:
        """Mark a batch run as finished with timestamp and final status."""
        run = self.session.get(ProcessingRunModel, run_id)
        if run:
            run.status = status.value if isinstance(status, RunStatus) else status
            run.completed_at = datetime.now(UTC)
            run.error_summary = error_summary
            self.session.flush()

    def get_run(self, run_id: str) -> ProcessingRunModel | None:
        """Fetch run details by ID."""
        return self.session.get(ProcessingRunModel, run_id)

    def list_recent_runs(self, limit: int = 10) -> list[ProcessingRunModel]:
        """Retrieve recent runs ordered by start time descending."""
        stmt = (
            select(ProcessingRunModel).order_by(ProcessingRunModel.started_at.desc()).limit(limit)
        )
        return list(self.session.scalars(stmt).all())

    # --- Enrichment Audit & Telemetry ---

    def record_attempt(
        self,
        run_id: str,
        company_id: str,
        stage: PipelineStage,
        status: AttemptStatus,
        attempt_number: int = 1,
        error_message: str | None = None,
        execution_time_ms: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> EnrichmentAttemptModel:
        """Record an audit trail entry for a specific stage attempt on a company."""
        attempt = EnrichmentAttemptModel(
            id=str(uuid4()),
            run_id=run_id,
            company_id=company_id,
            stage=stage.value if isinstance(stage, PipelineStage) else stage,
            status=status.value if isinstance(status, AttemptStatus) else status,
            attempt_number=attempt_number,
            error_message=error_message,
            execution_time_ms=execution_time_ms,
            metadata_json=metadata or {},
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    # --- Company Operations & Restartable Queries ---

    def upsert_company(
        self, company: CompanyCreate, canonical_fingerprint: str | None = None
    ) -> CompanyModel:
        """Insert or retrieve existing company matching canonical fingerprint."""
        if canonical_fingerprint:
            stmt = select(CompanyModel).where(
                CompanyModel.canonical_fingerprint == canonical_fingerprint
            )
            existing = self.session.scalars(stmt).first()
            if existing:
                return existing

        model = CompanyModel(
            id=company.id,
            name=company.name,
            raw_name=company.raw_name,
            city=company.city,
            state=company.state,
            category=company.category.value
            if isinstance(company.category, ContractorCategory)
            else company.category,
            website=company.website,
            email=company.email,
            status=company.status.value
            if isinstance(company.status, CompanyStatus)
            else company.status,
            confidence_score=company.confidence_score,
            canonical_fingerprint=canonical_fingerprint,
        )
        self.session.add(model)
        self.session.flush()
        return model

    def get_pending_companies_for_stage(
        self,
        stage: PipelineStage,
        category: str | None = None,
        state: str | None = None,
        limit: int = 500,
    ) -> list[CompanyModel]:
        """Fetch companies pending work for a given stage to allow seamless resume / restartability.

        Excludes companies that already have a SUCCESS attempt recorded for this stage.
        """
        # Subquery: company IDs that succeeded in this stage
        subq = select(EnrichmentAttemptModel.company_id).where(
            EnrichmentAttemptModel.stage
            == (stage.value if isinstance(stage, PipelineStage) else stage),
            EnrichmentAttemptModel.status == AttemptStatus.SUCCESS.value,
        )

        query = select(CompanyModel).where(~CompanyModel.id.in_(subq))

        if category:
            query = query.where(CompanyModel.category == category)
        if state:
            query = query.where(CompanyModel.state == state.upper())

        # Exclude rejected/excluded companies
        query = query.where(
            CompanyModel.status.not_in([CompanyStatus.EXCLUDED.value, CompanyStatus.REJECTED.value])
        ).limit(limit)

        return list(self.session.scalars(query).all())

    def update_company_status(
        self,
        company_id: str,
        status: CompanyStatus,
        website: str | None = None,
        email: str | None = None,
        confidence_score: float | None = None,
    ) -> None:
        """Update company status and optional verified website/email fields."""
        values: dict[str, Any] = {"status": status.value}
        if website is not None:
            values["website"] = website
        if email is not None:
            values["email"] = email
        if confidence_score is not None:
            values["confidence_score"] = confidence_score

        stmt = update(CompanyModel).where(CompanyModel.id == company_id).values(**values)
        self.session.execute(stmt)
        self.session.flush()

    def get_verified_leads(
        self,
        category: str | None = None,
        state: str | None = None,
        min_confidence: float = MIN_LEAD_CONFIDENCE_THRESHOLD,
    ) -> list[FinalLead]:
        """Query companies meeting all verified lead criteria:
        - Must have verified website (non-empty)
        - Must have verified email (non-empty)
        - Status is VERIFIED_LEAD
        - Confidence >= min_confidence
        """
        query = select(CompanyModel).where(
            CompanyModel.status == CompanyStatus.VERIFIED_LEAD.value,
            CompanyModel.website.is_not(None),
            CompanyModel.website != "",
            CompanyModel.email.is_not(None),
            CompanyModel.email != "",
            CompanyModel.confidence_score >= min_confidence,
        )
        if category:
            query = query.where(CompanyModel.category == category)
        if state:
            query = query.where(CompanyModel.state == state.upper())

        models = self.session.scalars(query).all()
        leads: list[FinalLead] = []
        for m in models:
            if m.website and m.email:
                leads.append(
                    FinalLead(
                        company_name=m.name,
                        email=m.email,
                        website=m.website,
                        city=m.city,
                        state=m.state,
                        category=ContractorCategory(m.category)
                        if m.category in ContractorCategory._value2member_map_
                        else None,
                        confidence_score=m.confidence_score,
                    )
                )
        return leads
