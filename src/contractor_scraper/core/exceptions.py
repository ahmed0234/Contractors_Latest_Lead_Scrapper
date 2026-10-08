"""Exception hierarchy for the contractor scraper and lead-generation engine."""

from typing import Any


class ContractorScraperError(Exception):
    """Base exception for all domain-specific errors in the contractor lead engine."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigurationError(ContractorScraperError):
    """Raised when application settings or environment variables are invalid or missing."""


class DatabaseError(ContractorScraperError):
    """Raised when database connection, query, or migration encounters an unrecoverable issue."""


class StorageError(ContractorScraperError):
    """Raised when persistent storage fails to read, write, or checkpoint data."""


class AcquisitionError(ContractorScraperError):
    """Raised during public state/company data acquisition failures."""


class ParsingError(ContractorScraperError):
    """Raised when raw acquired data cannot be parsed into structured records."""


class NormalizationError(ContractorScraperError):
    """Raised during company name, address, or geographic normalization failures."""


class ClassificationError(ContractorScraperError):
    """Raised during industry filtering and contractor category classification."""


class DiscoveryError(ContractorScraperError):
    """Raised during official website search or candidate discovery."""


class ExtractionError(ContractorScraperError):
    """Raised during business email extraction from web targets."""


class VerificationError(ContractorScraperError):
    """Raised during website or email verification steps."""


class RecordProcessingError(ContractorScraperError):
    """Non-fatal error encountered while processing an individual company record.

    Ensures that an error on one record does NOT crash the entire batch pipeline.
    """

    def __init__(
        self,
        message: str,
        company_id: str | None = None,
        stage: str | None = None,
        is_retryable: bool = True,
        cause: Exception | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        details = details or {}
        if company_id:
            details["company_id"] = company_id
        if stage:
            details["stage"] = stage
        details["is_retryable"] = is_retryable
        if cause:
            details["cause"] = str(cause)

        super().__init__(message, details)
        self.company_id = company_id
        self.stage = stage
        self.is_retryable = is_retryable
        self.cause = cause


class PipelineStageError(ContractorScraperError):
    """Fatal or stage-halting error indicating that a pipeline stage failed completely."""

    def __init__(
        self,
        message: str,
        stage: str,
        run_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        details = details or {}
        details["stage"] = stage
        if run_id:
            details["run_id"] = run_id
        super().__init__(message, details)
        self.stage = stage
        self.run_id = run_id
