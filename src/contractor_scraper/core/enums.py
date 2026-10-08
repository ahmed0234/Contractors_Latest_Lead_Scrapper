from enum import StrEnum


class ContractorCategory(StrEnum):
    """Supported contractor industries/categories for targeted lead generation."""

    ROOFING = "Roofing"
    HVAC = "HVAC"
    PLUMBING = "Plumbing"
    LANDSCAPING = "Landscaping"
    HARDSCAPING = "Hardscaping"
    REMODELING = "Remodeling"
    PEST_CONTROL = "Pest Control"
    MED_SPA = "Med Spa"


class CompanyStatus(StrEnum):
    """Lifecycle states of a company record through the enrichment pipeline."""

    DISCOVERED = "DISCOVERED"
    NORMALIZED = "NORMALIZED"
    CLASSIFIED = "CLASSIFIED"
    WEBSITE_FOUND = "WEBSITE_FOUND"
    EMAIL_FOUND = "EMAIL_FOUND"
    VERIFIED_LEAD = "VERIFIED_LEAD"
    EXCLUDED = "EXCLUDED"
    REJECTED = "REJECTED"


class WebsiteStatus(StrEnum):
    """Verification status of a discovered business website candidate."""

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    UNREACHABLE = "UNREACHABLE"
    PARKED_DOMAIN = "PARKED_DOMAIN"
    DIRECTORY_PAGE = "DIRECTORY_PAGE"
    REJECTED = "REJECTED"


class EmailStatus(StrEnum):
    """Verification status of a discovered business email address."""

    PENDING = "PENDING"
    VALID_SYNTAX = "VALID_SYNTAX"
    MX_VERIFIED = "MX_VERIFIED"
    DELIVERABLE = "DELIVERABLE"
    DISPOSABLE = "DISPOSABLE"
    CATCH_ALL = "CATCH_ALL"
    REJECTED = "REJECTED"


class RunStatus(StrEnum):
    """Execution status for a pipeline processing batch run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


class AttemptStatus(StrEnum):
    """Outcome status for individual enrichment attempts on a record."""

    SUCCESS = "SUCCESS"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    FATAL_ERROR = "FATAL_ERROR"
    SKIPPED = "SKIPPED"


class PipelineStage(StrEnum):
    """Distinct architectural stages in the lead-generation workflow."""

    ACQUISITION = "acquisition"
    PARSING = "parsing"
    NORMALIZATION = "normalization"
    CLASSIFICATION = "classification"
    MATCHING = "matching"
    WEBSITE_DISCOVERY = "website_discovery"
    WEBSITE_VERIFICATION = "website_verification"
    EMAIL_EXTRACTION = "email_extraction"
    EMAIL_VERIFICATION = "email_verification"
    DEDUPLICATION = "deduplication"
    EXPORTING = "exporting"
