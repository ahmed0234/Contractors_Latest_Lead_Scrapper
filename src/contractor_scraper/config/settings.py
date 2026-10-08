"""Configuration management using Pydantic Settings and environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from contractor_scraper.core.constants import DEFAULT_USER_AGENT, US_STATES
from contractor_scraper.core.enums import ContractorCategory


class Settings(BaseSettings):
    """Central configuration class for the contractor lead engine."""

    model_config = SettingsConfigDict(
        env_prefix="SCRAPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Target Execution Defaults ---
    selected_category: ContractorCategory | None = Field(
        default=None,
        description="Target contractor industry (e.g. Roofing, HVAC, Plumbing)",
    )
    selected_state: str | None = Field(
        default=None,
        description="Target 2-letter U.S. state postal code (e.g. TX, CA, FL)",
    )

    # --- Performance & Concurrency Settings ---
    concurrency_limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum concurrent async workers for I/O operations",
    )
    request_timeout_seconds: float = Field(
        default=15.0,
        gt=0.0,
        le=120.0,
        description="Request timeout in seconds for HTTP network calls",
    )
    retry_count: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum retries upon transient network or rate-limit errors",
    )
    rate_limit_per_second: float = Field(
        default=5.0,
        gt=0.0,
        le=50.0,
        description="Max requests dispatched per second to prevent rate abuse",
    )
    user_agent: str = Field(
        default=DEFAULT_USER_AGENT,
        description="HTTP User-Agent header for public data acquisition",
    )

    # --- Search Engine & Enrichment API Settings ---
    google_search_api_key: str | None = Field(
        default=None,
        description="Optional Google Custom Search JSON API Key",
    )
    google_search_cx: str | None = Field(
        default=None,
        description="Optional Google Custom Search Engine ID (cx)",
    )
    serper_api_key: str | None = Field(
        default=None,
        description="Optional Serper.dev Google Search API Key",
    )
    serpapi_api_key: str | None = Field(
        default=None,
        description="Optional SerpApi API Key",
    )

    # --- Storage & File Paths ---
    database_path: Path = Field(
        default=Path("data/contractor_engine.db"),
        description="Persistent SQLite database path for transactional state and checkpoints",
    )
    duckdb_path: Path = Field(
        default=Path("data/analytics.duckdb"),
        description="DuckDB analytical storage path for high-throughput queries and Parquet exports",
    )
    raw_data_dir: Path = Field(
        default=Path("data/raw"),
        description="Directory for raw acquired data dumps (e.g. SOS CSVs, JSON)",
    )
    processed_data_dir: Path = Field(
        default=Path("data/processed"),
        description="Directory for parsed, normalized, and intermediate data files",
    )
    output_dir: Path = Field(
        default=Path("data/output"),
        description="Directory for final exported verified leads (CSV, Parquet, JSON)",
    )
    cache_dir: Path = Field(
        default=Path("data/cache"),
        description="Directory for caching HTTP responses and search metadata",
    )

    # --- Logging & Diagnostics ---
    log_level: str = Field(
        default="INFO",
        description="Application logging verbosity: DEBUG, INFO, WARNING, ERROR, CRITICAL",
    )
    logs_dir: Path = Field(
        default=Path("logs"),
        description="Directory where log files are stored",
    )
    json_log_enabled: bool = Field(
        default=True,
        description="Whether to format file logs as structured JSON lines",
    )

    @field_validator("selected_state")
    @classmethod
    def validate_state(cls, v: str | None) -> str | None:
        """Validate that state is a valid 2-letter US code if provided."""
        if v is None:
            return None
        cleaned = v.strip().upper()
        if cleaned not in US_STATES:
            raise ValueError(
                f"Invalid state code '{v}'. Must be a valid 2-letter U.S. postal code (e.g. TX, CA)."
            )
        return cleaned

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level string."""
        cleaned = v.strip().upper()
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if cleaned not in valid_levels:
            raise ValueError(f"Invalid log level '{v}'. Must be one of {valid_levels}")
        return cleaned

    def ensure_directories(self) -> None:
        """Create all configured workspace directories if they do not yet exist."""
        for path in (
            self.raw_data_dir,
            self.processed_data_dir,
            self.output_dir,
            self.cache_dir,
            self.logs_dir,
            self.database_path.parent,
            self.duckdb_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve cached singleton Settings instance."""
    return Settings()
