"""CLI application entrypoint using Typer."""

import typer

from contractor_scraper.cli.commands import (
    christmas_sunbiz_command,
    classify_christmas_lights_command,
    enrich_pest_control_command,
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
        help="Contractor category (e.g. roofing, hvac, plumbing, landscaping, hardscaping, remodeling, pest-control, med-spa, mattress, spa)",
    ),
    keyword: str | None = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Contractor category/keyword alias (e.g. roofing, hvac, plumbing)",
    ),
    roofing: bool = typer.Option(
        False,
        "--roofing",
        help="Direct shortcut to target Roofing contractors",
    ),
    hvac: bool = typer.Option(
        False,
        "--hvac",
        help="Direct shortcut to target HVAC contractors",
    ),
    plumbing: bool = typer.Option(
        False,
        "--plumbing",
        help="Direct shortcut to target Plumbing contractors",
    ),
    landscaping: bool = typer.Option(
        False,
        "--landscaping",
        help="Direct shortcut to target Landscaping contractors",
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
        if not target_category:
            if roofing:
                target_category = "roofing"
            elif hvac:
                target_category = "hvac"
            elif plumbing:
                target_category = "plumbing"
            elif landscaping:
                target_category = "landscaping"

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
        help="Contractor category (e.g. roofing, hvac, plumbing, landscaping, hardscaping, remodeling, pest-control, med-spa, mattress, spa)",
    ),
    keyword: str | None = typer.Option(
        None,
        "--keyword",
        "-k",
        help="Contractor category/keyword alias (e.g. roofing, hvac, plumbing)",
    ),
    roofing: bool = typer.Option(
        False,
        "--roofing",
        help="Direct shortcut to target Roofing contractors",
    ),
    hvac: bool = typer.Option(
        False,
        "--hvac",
        help="Direct shortcut to target HVAC contractors",
    ),
    plumbing: bool = typer.Option(
        False,
        "--plumbing",
        help="Direct shortcut to target Plumbing contractors",
    ),
    landscaping: bool = typer.Option(
        False,
        "--landscaping",
        help="Direct shortcut to target Landscaping contractors",
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
    if not target_category:
        if roofing:
            target_category = "roofing"
        elif hvac:
            target_category = "hvac"
        elif plumbing:
            target_category = "plumbing"
        elif landscaping:
            target_category = "landscaping"

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


@app.command(
    name="enrich-pest-control",
    help="Enrich an existing Pest Control CSV with verified official websites and trade classifications.",
)
def enrich_pest_control(
    input_file: str = typer.Option(
        "input_files/Pest Control - James  - Final Raw Leads.csv",
        "--input",
        "-i",
        help="Path to input CSV containing pest control companies.",
    ),
    output_file: str = typer.Option(
        "data/output/enriched_pest_control_leads.csv",
        "--output",
        "-o",
        help="Path to save enriched output CSV file.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-l",
        help="Maximum records to process (for testing or sampling).",
    ),
    concurrency: int = typer.Option(
        5,
        "--concurrency",
        "-c",
        help="Maximum concurrent asynchronous website discovery & crawl workers.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Resume processing from already-enriched output file if it exists.",
    ),
) -> None:
    enrich_pest_control_command(
        input_path=input_file,
        output_path=output_file,
        limit=limit,
        concurrency=concurrency,
        resume=resume,
    )


@app.command(
    name="classify-christmas-lights",
    help="Clean and classify Christmas light installation contractor leads from company websites.",
)
def classify_christmas_lights(
    input_file: str = typer.Option(
        "christmas light installation/Christmas Lights Installation.csv",
        "--input",
        "-i",
        help="Path to input CSV containing Christmas light contractor candidates.",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output",
        "--output-dir",
        "-o",
        help="Directory for output CSV files (default: <input folder>/output/<input file name>).",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-l",
        help="Maximum records to process (for testing or sampling).",
    ),
    concurrency: int = typer.Option(
        8,
        "--concurrency",
        "-c",
        help="Maximum websites crawled concurrently.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help="Resume processing from already-evaluated output file if it exists.",
    ),
    timeout: float = typer.Option(
        20.0,
        "--timeout",
        help="Per-request read timeout in seconds (connect and whole-site budgets scale from it).",
    ),
) -> None:
    classify_christmas_lights_command(
        input_path=input_file,
        output_dir=output_dir,
        limit=limit,
        concurrency=concurrency,
        resume=resume,
        timeout=timeout,
    )



@app.command(
    name="christmas-sunbiz",
    help="Find verified Christmas / holiday lighting installers from Sunbiz filings (resumable).",
)
def christmas_sunbiz_cli(
    output: str | None = typer.Option(None, "--output", "-o", help="Leads CSV path."),
    year: list[int] | None = typer.Option(None, "--year", "-y", help="Sunbiz year(s) to scan (repeatable)."),  # noqa: B008
    download: bool = typer.Option(False, "--download", help="Fetch missing Sunbiz files from the portal."),
    scope: str = typer.Option("focused", "--scope", help="holiday | focused | broad"),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Max registrations researched this run."),
    max_leads: int | None = typer.Option(None, "--max-leads", help="Stop after N new qualified leads."),
    concurrency: int = typer.Option(6, "--concurrency", "-c", help="Registrations researched in parallel."),
    resume: bool = typer.Option(True, "--resume/--no-resume", help="Skip registrations already researched."),
) -> None:
    christmas_sunbiz_command(
        output=output,
        years=year or None,
        download=download,
        scope=scope,
        limit=limit,
        max_leads=max_leads,
        concurrency=concurrency,
        resume=resume,
    )


if __name__ == "__main__":
    app()
