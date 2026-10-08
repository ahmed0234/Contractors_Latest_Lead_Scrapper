"""Shared Pytest fixtures and test helpers for unit and integration tests."""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from contractor_scraper.config.settings import Settings
from contractor_scraper.core.enums import ContractorCategory
from contractor_scraper.storage.database import init_db
from contractor_scraper.storage.models import Base


def make_sunbiz_line(
    corp_num: str = "P25000001234",
    name: str = "APEX ROOFING CONTRACTORS LLC",
    status: str = "A",
    filing_type: str = "FLAL",
    addr1: str = "123 MAIN ST",
    city: str = "ORLANDO",
    state: str = "FL",
    zip_code: str = "32801",
    file_date: str = "01152025",
) -> str:
    """Helper creating a valid 1440-character fixed-width Sunbiz line."""
    line = [" "] * 1440
    for i, c in enumerate(corp_num[:12]):
        line[i] = c
    for i, c in enumerate(name[:192]):
        line[12 + i] = c
    line[204] = status
    for i, c in enumerate(filing_type[:15]):
        line[205 + i] = c
    for i, c in enumerate(addr1[:42]):
        line[220 + i] = c
    for i, c in enumerate(city[:28]):
        line[304 + i] = c
    for i, c in enumerate(state[:2]):
        line[332 + i] = c
    for i, c in enumerate(zip_code[:10]):
        line[334 + i] = c
    for i, c in enumerate(file_date[:8]):
        line[472 + i] = c
    return "".join(line)


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> dict[str, Path]:
    """Provides isolated temporary paths for testing."""
    paths = {
        "root": tmp_path,
        "database": tmp_path / "test.db",
        "duckdb": tmp_path / "test.duckdb",
        "raw": tmp_path / "raw",
        "processed": tmp_path / "processed",
        "output": tmp_path / "output",
        "cache": tmp_path / "cache",
        "logs": tmp_path / "logs",
    }
    for p in paths.values():
        if p.suffix:
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            p.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture
def test_settings(tmp_workspace: dict[str, Path]) -> Settings:
    """Configured test settings pointing to temporary directories."""
    return Settings(
        selected_category=ContractorCategory.ROOFING,
        selected_state="FL",
        database_path=tmp_workspace["database"],
        duckdb_path=tmp_workspace["duckdb"],
        raw_data_dir=tmp_workspace["raw"],
        processed_data_dir=tmp_workspace["processed"],
        output_dir=tmp_workspace["output"],
        cache_dir=tmp_workspace["cache"],
        logs_dir=tmp_workspace["logs"],
        log_level="DEBUG",
        concurrency_limit=5,
    )


@pytest.fixture
def test_engine() -> Generator[Engine, None, None]:
    """In-memory SQLite engine for isolated storage testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    init_db(engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def test_session(test_engine: Engine) -> Generator[Session, None, None]:
    """Isolated database session backed by test_engine."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False)
    session = factory()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def sample_company_payload() -> dict[str, Any]:
    """Sample valid company payload dictionary."""
    return {
        "name": "Apex Roofing Solutions",
        "raw_name": "Apex Roofing Solutions, LLC",
        "city": "Orlando",
        "state": "FL",
        "category": ContractorCategory.ROOFING,
        "website": "https://apexroofingorlando.com",
        "email": "contact@apexroofingorlando.com",
    }
