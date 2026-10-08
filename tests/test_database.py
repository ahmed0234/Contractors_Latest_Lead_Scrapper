"""Tests for database engine setup, table initialization, and transactions."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from contractor_scraper.core.exceptions import DatabaseError
from contractor_scraper.storage.database import create_db_engine, get_db_session, init_db
from contractor_scraper.storage.models import CompanyModel


def test_init_db_creates_all_tables() -> None:
    """Verify all 6 core tables are created by init_db."""
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    expected_tables = {
        "companies",
        "source_records",
        "websites",
        "emails",
        "processing_runs",
        "enrichment_attempts",
    }
    assert expected_tables.issubset(table_names)
    engine.dispose()


def test_create_db_engine_file_path(tmp_path: Path) -> None:
    """Verify engine creation with local SQLite file path."""
    db_file = tmp_path / "custom" / "test.db"
    engine = create_db_engine(db_file)
    assert db_file.parent.exists()
    init_db(engine)

    inspector = inspect(engine)
    assert "companies" in inspector.get_table_names()
    engine.dispose()


def test_get_db_session_transaction_commit(test_engine) -> None:
    """Verify transaction commits on normal completion."""
    with get_db_session(engine=test_engine) as session:
        comp = CompanyModel(
            id="test-1",
            name="Reliable Roofing",
            city="Houston",
            state="TX",
            category="Roofing",
        )
        session.add(comp)

    # Re-open session and verify record persisted
    with get_db_session(engine=test_engine) as session:
        loaded = session.get(CompanyModel, "test-1")
        assert loaded is not None
        assert loaded.name == "Reliable Roofing"


def test_get_db_session_rollback_on_exception(test_engine) -> None:
    """Verify session rolls back and raises DatabaseError on exception."""
    with pytest.raises(DatabaseError):
        with get_db_session(engine=test_engine) as session:
            comp = CompanyModel(
                id="test-rollback",
                name="Failing Roofing",
                city="Dallas",
                state="TX",
                category="Roofing",
            )
            session.add(comp)
            raise ValueError("Forced error to trigger rollback")

    # Verify record was not persisted
    with get_db_session(engine=test_engine) as session:
        loaded = session.get(CompanyModel, "test-rollback")
        assert loaded is None
