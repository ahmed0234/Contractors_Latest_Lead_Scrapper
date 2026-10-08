"""Tests for configuration loading, validation, and defaults."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from contractor_scraper.config.settings import Settings
from contractor_scraper.core.enums import ContractorCategory


def test_default_settings() -> None:
    """Verify sensible defaults are populated without an explicit .env file."""
    settings = Settings()
    assert settings.concurrency_limit == 10
    assert settings.request_timeout_seconds == 15.0
    assert settings.retry_count == 3
    assert settings.rate_limit_per_second == 5.0
    assert settings.database_path == Path("data/contractor_engine.db")
    assert settings.log_level == "INFO"
    assert settings.json_log_enabled is True


def test_custom_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify settings can be overridden via SCRAPER_ environment variables."""
    monkeypatch.setenv("SCRAPER_SELECTED_CATEGORY", "HVAC")
    monkeypatch.setenv("SCRAPER_SELECTED_STATE", "FL")
    monkeypatch.setenv("SCRAPER_CONCURRENCY_LIMIT", "25")
    monkeypatch.setenv("SCRAPER_LOG_LEVEL", "DEBUG")

    settings = Settings()
    assert settings.selected_category == ContractorCategory.HVAC
    assert settings.selected_state == "FL"
    assert settings.concurrency_limit == 25
    assert settings.log_level == "DEBUG"


def test_invalid_state_validation() -> None:
    """Ensure invalid 2-letter state codes are rejected."""
    with pytest.raises(ValidationError):
        Settings(selected_state="ZZ")


def test_invalid_log_level_validation() -> None:
    """Ensure unsupported log levels are rejected."""
    with pytest.raises(ValidationError):
        Settings(log_level="VERBOSE")


def test_ensure_directories(tmp_path: Path) -> None:
    """Ensure ensure_directories creates all configured directories."""
    settings = Settings(
        raw_data_dir=tmp_path / "raw",
        processed_data_dir=tmp_path / "processed",
        output_dir=tmp_path / "output",
        cache_dir=tmp_path / "cache",
        logs_dir=tmp_path / "logs",
        database_path=tmp_path / "db" / "engine.db",
        duckdb_path=tmp_path / "db" / "analytics.duckdb",
    )
    settings.ensure_directories()
    assert (tmp_path / "raw").exists()
    assert (tmp_path / "processed").exists()
    assert (tmp_path / "output").exists()
    assert (tmp_path / "cache").exists()
    assert (tmp_path / "logs").exists()
    assert (tmp_path / "db").exists()
