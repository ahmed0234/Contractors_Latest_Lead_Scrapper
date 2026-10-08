"""Domain models for pipeline processing runs, checkpoints, and enrichment attempts."""

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field

from contractor_scraper.core.enums import (
    AttemptStatus,
    ContractorCategory,
    PipelineStage,
    RunStatus,
)
from contractor_scraper.models.base import BaseSchema, current_utc_time


class ProcessingRunBase(BaseSchema):
    """Execution state and telemetry metrics for a pipeline run."""

    category: ContractorCategory = Field(..., description="Target contractor industry")
    state: str | None = Field(
        None, max_length=2, description="Target U.S. State or None for nationwide"
    )
    status: RunStatus = Field(default=RunStatus.PENDING)
    total_records: int = Field(default=0, ge=0)
    processed_records: int = Field(default=0, ge=0)
    successful_records: int = Field(default=0, ge=0)
    failed_records: int = Field(default=0, ge=0)
    skipped_records: int = Field(default=0, ge=0)
    error_summary: str | None = None


class ProcessingRunCreate(ProcessingRunBase):
    """Schema for initializing a new batch execution run."""

    id: str = Field(default_factory=lambda: str(uuid4()))


class ProcessingRunUpdate(BaseSchema):
    """Schema for updating run status or intermediate checkpoint counters."""

    status: RunStatus | None = None
    total_records: int | None = None
    processed_records: int | None = None
    successful_records: int | None = None
    failed_records: int | None = None
    skipped_records: int | None = None
    error_summary: str | None = None
    completed_at: datetime | None = None


class ProcessingRunRead(ProcessingRunBase):
    """Schema for reading a completed or active run record."""

    id: str
    started_at: datetime = Field(default_factory=current_utc_time)
    completed_at: datetime | None = None


class EnrichmentAttemptBase(BaseSchema):
    """Audit log entry capturing individual stage attempts per company."""

    run_id: str = Field(..., description="Foreign key to ProcessingRun")
    company_id: str = Field(..., description="Foreign key to Company")
    stage: PipelineStage = Field(..., description="Pipeline stage attempted")
    status: AttemptStatus = Field(default=AttemptStatus.SUCCESS)
    attempt_number: int = Field(default=1, ge=1)
    error_message: str | None = None
    execution_time_ms: float = Field(default=0.0, ge=0.0)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class EnrichmentAttemptCreate(EnrichmentAttemptBase):
    """Schema for recording an enrichment attempt."""

    id: str = Field(default_factory=lambda: str(uuid4()))


class EnrichmentAttemptRead(EnrichmentAttemptBase):
    """Schema for reading an enrichment attempt record."""

    id: str
    created_at: datetime = Field(default_factory=current_utc_time)
