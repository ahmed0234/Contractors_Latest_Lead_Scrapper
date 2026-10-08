"""Tests for CLI startup, subcommands, and flags."""

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from contractor_scraper.cli.app import app
from contractor_scraper.orchestration.lead_engine import EngineRunTelemetry

runner = CliRunner()


def test_cli_help() -> None:
    """Verify CLI starts up and prints available commands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "High-Quality U.S. Contractor Lead-Generation Engine CLI" in result.output
    assert "status" in result.output
    assert "init-db" in result.output
    assert "run" in result.output


def test_cli_status() -> None:
    """Verify CLI status command outputs settings."""
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "System Configuration & Settings" in result.output
    assert "Concurrency Limit" in result.output


def test_cli_init_db() -> None:
    """Verify CLI init-db command initializes database tables."""
    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0
    assert "Database successfully initialized!" in result.output
    assert "companies" in result.output


def test_cli_inspect_runs() -> None:
    """Verify CLI inspect-runs command executes."""
    result = runner.invoke(app, ["inspect-runs"])
    assert result.exit_code == 0


def test_cli_run_invalid_category() -> None:
    """Verify CLI rejects unsupported contractor category with clear error."""
    result = runner.invoke(app, ["run", "--year", "2025", "--category", "space-mining"])
    assert result.exit_code != 0
    assert "Unsupported category" in result.output


def test_cli_run_invalid_year() -> None:
    """Verify CLI rejects out-of-range year."""
    result = runner.invoke(app, ["run", "--year", "1850", "--category", "roofing"])
    assert result.exit_code != 0
    assert "Invalid year" in result.output


def test_cli_run_invalid_limit() -> None:
    """Verify CLI rejects non-positive limit."""
    result = runner.invoke(app, ["run", "--year", "2025", "--category", "roofing", "--limit", "0"])
    assert result.exit_code != 0
    assert "--limit must be a positive integer" in result.output


def test_cli_root_command_with_flags() -> None:
    """Verify invoking root command directly with --year, --category, and --limit executes engine."""
    mock_telemetry = EngineRunTelemetry(
        year=2025,
        category="Roofing",
        requested_limit=10,
        raw_records_examined=50,
        category_candidates=5,
        websites_discovered=5,
        websites_verified=4,
        emails_discovered=4,
        final_complete_leads=4,
        output_csv_path=None,
    )

    with patch(
        "contractor_scraper.orchestration.lead_engine.LeadEngine.run", new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = (None, mock_telemetry)
        result = runner.invoke(app, ["--year", "2025", "--category", "roofing", "--limit", "10"])
        assert result.exit_code == 0
        assert "Execution Summary & Lead Telemetry" in result.output
        assert "Roofing" in result.output
