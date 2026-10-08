"""SQLAlchemy ORM models for relational persistence, checkpointing, and audit trails."""

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base declarative class for all database tables."""

    pass


class CompanyModel(Base):
    """Normalized company record."""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    raw_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(2), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), index=True, default="DISCOVERED", nullable=False
    )
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    canonical_fingerprint: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    # Relationships
    source_records: Mapped[list["SourceRecordModel"]] = relationship(
        "SourceRecordModel", back_populates="company", cascade="all, delete-orphan"
    )
    websites: Mapped[list["WebsiteModel"]] = relationship(
        "WebsiteModel", back_populates="company", cascade="all, delete-orphan"
    )
    emails: Mapped[list["EmailModel"]] = relationship(
        "EmailModel", back_populates="company", cascade="all, delete-orphan"
    )
    enrichment_attempts: Mapped[list["EnrichmentAttemptModel"]] = relationship(
        "EnrichmentAttemptModel", back_populates="company", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_company_state_category", "state", "category"),
        Index("idx_company_status_category", "status", "category"),
    )


class SourceRecordModel(Base):
    """Raw public record acquired from state databases or public registers."""

    __tablename__ = "source_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_name: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(150), index=True, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    processed_status: Mapped[str] = mapped_column(String(50), default="UNPROCESSED", nullable=False)

    company: Mapped[Optional["CompanyModel"]] = relationship(
        "CompanyModel", back_populates="source_records"
    )

    __table_args__ = (
        Index("idx_source_unique_record", "source_name", "source_record_id", unique=True),
    )


class WebsiteModel(Base):
    """Website discovery candidate and verification telemetry."""

    __tablename__ = "websites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discovered_via: Mapped[str] = mapped_column(String(100), default="search", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    company: Mapped["CompanyModel"] = relationship("CompanyModel", back_populates="websites")


class EmailModel(Base):
    """Extracted business email address and deliverability state."""

    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email_address: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(50), default="PENDING", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    company: Mapped["CompanyModel"] = relationship("CompanyModel", back_populates="emails")


class ProcessingRunModel(Base):
    """Batch pipeline run for orchestrating and checkpointing executions."""

    __tablename__ = "processing_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    state: Mapped[str | None] = mapped_column(String(2), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(50), index=True, default="PENDING", nullable=False)
    total_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_records: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    enrichment_attempts: Mapped[list["EnrichmentAttemptModel"]] = relationship(
        "EnrichmentAttemptModel", back_populates="run", cascade="all, delete-orphan"
    )


class EnrichmentAttemptModel(Base):
    """Audit log entry capturing individual stage outcomes for each company."""

    __tablename__ = "enrichment_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("processing_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    company_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("companies.id", ondelete="CASCADE"), index=True, nullable=False
    )
    stage: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(50), index=True, default="SUCCESS", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_time_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    run: Mapped["ProcessingRunModel"] = relationship(
        "ProcessingRunModel", back_populates="enrichment_attempts"
    )
    company: Mapped["CompanyModel"] = relationship(
        "CompanyModel", back_populates="enrichment_attempts"
    )

    __table_args__ = (Index("idx_attempt_run_stage", "run_id", "stage"),)
