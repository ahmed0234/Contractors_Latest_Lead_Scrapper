"""CLI application entrypoint using Typer."""

import typer

from contractor_scraper.cli.commands import (
    execute_engine_cli,
    init_db_command,
    inspect_runs_command,
    status_command,
)

app = typer.Typer(
    name="contractor-scraper",
    help="High-Quality U.S. Contractor Lead-Generation Engine CLI.",
    add_completion=False,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    year: int | None = typer.Option(
        None,
        "--year",
        "-y",
        help="Sunbiz filing data year (e.g. 2025, 2024)",
    ),
    category: str | None = typer.Option(
        None,
        "--category",
        "-c",
        help="Contractor category (e.g. roofing, hvac, plumbing, landscaping, hardscaping, remodeling, pest-control, med-spa)",
    ),
    keyword: str | None = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Contractor category/keyword alias (e.g. roofing, hvac, plumbing)",
    ),
    limit: int = typer.Option(
        10000,
        "--limit",
        "-l",
        help="Maximum verified complete leads to produce (default: 10000)",
    ),
    max_files: int | None = typer.Option(
        None,
        "--max-files",
        help="Maximum source filing files to process (useful for sampling or test runs)",
    ),
) -> None:
    """Invoked when no subcommand is given. Executes engine or defaults to interactive prompts."""
    if ctx.invoked_subcommand is None:
        target_category = category or keyword
        execute_engine_cli(
            year=year,
            category=target_category,
            limit=limit,
            max_source_files=max_files,
        )


@app.command(
    name="run",
    help="Run the lead-generation pipeline with options.",
)
def run_command(
    year: int | None = typer.Option(
        None,
        "--year",
        "-y",
        help="Sunbiz filing data year (e.g. 2025, 2024)",
    ),
    category: str | None = typer.Option(
        None,
        "--category",
        "-c",
        help="Contractor category (e.g. roofing, hvac, plumbing, landscaping, hardscaping, remodeling, pest-control, med-spa)",
    ),
    keyword: str | None = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Contractor category/keyword alias (e.g. roofing, hvac, plumbing)",
    ),
    limit: int = typer.Option(
        10000,
        "--limit",
        "-l",
        help="Maximum verified complete leads to produce (default: 10000)",
    ),
    max_files: int | None = typer.Option(
        None,
        "--max-files",
        help="Maximum source filing files to process (useful for sampling or test runs)",
    ),
) -> None:
    target_category = category or keyword
    execute_engine_cli(
        year=year,
        category=target_category,
        limit=limit,
        max_source_files=max_files,
    )


@app.command(
    name="status",
    help="Display current system settings, database status, and directory layout.",
)
def status() -> None:
    status_command()


@app.command(
    name="init-db",
    help="Initialize persistent database storage and tables.",
)
def init_database() -> None:
    init_db_command()


@app.command(
    name="inspect-runs",
    help="Inspect recent pipeline execution checkpoints and metrics.",
)
def inspect_runs() -> None:
    inspect_runs_command()


if __name__ == "__main__":
    app()
