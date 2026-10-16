"""CLI commands and interactive user prompts for the contractor lead engine."""

import asyncio
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from contractor_scraper.classification.classifier import get_classifier
from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import setup_logging
from contractor_scraper.orchestration.lead_engine import EngineRunTelemetry, LeadEngine
from contractor_scraper.storage.database import get_db_session, get_engine, init_db
from contractor_scraper.storage.models import Base
from contractor_scraper.storage.repository import LeadRepository

console = Console()

SUPPORTED_CATEGORIES: list[str] = [
    "roofing",
    "hvac",
    "plumbing",
    "landscaping",
    "hardscaping",
    "remodeling",
    "pest-control",
    "med-spa",
    "mattress",
    "spa",
]


def prompt_contractor_category() -> str:
    """Interactively prompt user to select one contractor category from the numbered menu."""
    table = Table(
        title="Available Contractor Categories", header_style="bold magenta", border_style="dim"
    )
    table.add_column("#", justify="center", style="cyan", no_wrap=True)
    table.add_column("Category Slug", style="bold green")
    table.add_column("Display Name", style="white")

    for idx, slug in enumerate(SUPPORTED_CATEGORIES, start=1):
        classifier = get_classifier(slug)
        table.add_row(str(idx), slug, classifier.category.value)

    console.print(table)

    while True:
        choice = (
            Prompt.ask(
                "[bold yellow]What contractor category do you want?[/bold yellow]",
                default="1",
            )
            .strip()
            .lower()
        )

        # Handle numbered selection
        if choice.isdigit():
            num = int(choice)
            if 1 <= num <= len(SUPPORTED_CATEGORIES):
                selected = SUPPORTED_CATEGORIES[num - 1]
                console.print(
                    f"[bold green]Selected Category:[/bold green] [bold cyan]{selected}[/bold cyan]"
                )
                return selected

        # Handle slug or name selection
        for slug in SUPPORTED_CATEGORIES:
            classifier = get_classifier(slug)
            if choice in (slug, classifier.category.value.lower()):
                console.print(
                    f"[bold green]Selected Category:[/bold green] [bold cyan]{slug}[/bold cyan]"
                )
                return slug

        console.print(
            f"[red]Invalid selection '{choice}'. Please enter a number (1-{len(SUPPORTED_CATEGORIES)}) or category name.[/red]"
        )


def prompt_year() -> int | None:
    """Interactively prompt for filing year, allowing default auto multi-year."""
    current_year = max(2026, datetime.now().year)
    while True:
        year_input = Prompt.ask(
            f"[bold yellow]What Sunbiz filing year do you want? (Press Enter for auto multi-year {current_year}->2016)[/bold yellow]",
            default="auto",
        ).strip()

        if year_input.lower() in ("auto", "all", "", "none"):
            return None

        if year_input.isdigit():
            val = int(year_input)
            if 1990 <= val <= current_year + 1:
                return val

        console.print(f"[red]Please enter a valid year between 1990 and {current_year + 1}, or press Enter for auto.[/red]")


def print_final_run_summary(telemetry: EngineRunTelemetry) -> None:
    """Print the final run execution summary table according to system requirements."""
    web_rate = (
        (telemetry.websites_verified / telemetry.category_candidates * 100)
        if telemetry.category_candidates > 0
        else 0.0
    )
    email_rate = (
        (telemetry.emails_discovered / telemetry.websites_verified * 100)
        if telemetry.websites_verified > 0
        else 0.0
    )
    prod_rate = (
        (telemetry.final_complete_leads / telemetry.category_candidates * 100)
        if telemetry.category_candidates > 0
        else 0.0
    )

    summary_table = Table(
        title="Execution Summary & Lead Telemetry",
        header_style="bold white on blue",
        border_style="cyan",
    )
    summary_table.add_column("Metric", style="bold white")
    summary_table.add_column("Count / Value", style="bold green", justify="right")

    summary_table.add_row("Filing Year", str(telemetry.year))
    summary_table.add_row("Contractor Category", telemetry.category)
    summary_table.add_row("Requested Limit", f"{telemetry.requested_limit:,}")
    summary_table.add_row("Raw Records Examined", f"{telemetry.raw_records_examined:,}")
    summary_table.add_row("Category Candidates Identified", f"{telemetry.category_candidates:,}")
    summary_table.add_row("Website Candidates Discovered", f"{telemetry.websites_discovered:,}")
    summary_table.add_row("Websites Verified", f"{telemetry.websites_verified:,}")
    summary_table.add_row("Emails Discovered", f"{telemetry.emails_discovered:,}")
    summary_table.add_row("Website Discovery & Verification Rate", f"{web_rate:.1f}%")
    summary_table.add_row("Email Discovery Rate", f"{email_rate:.1f}%")
    summary_table.add_row(
        "Production Leads Generated",
        f"[bold green]{telemetry.final_complete_leads:,}[/bold green]",
    )
    summary_table.add_row(
        "Production Lead Conversion Rate",
        f"[bold green]{prod_rate:.1f}%[/bold green]",
    )
    summary_table.add_row(
        "Leads Missing Contact (Research List)",
        f"[bold yellow]{telemetry.missing_contact_leads:,}[/bold yellow]",
    )
    summary_table.add_row("Excluded Records", f"{telemetry.rejected_records:,}")
    summary_table.add_row("Execution Duration", f"{telemetry.duration_seconds:.2f}s")
    summary_table.add_row(
        "Production Leads CSV",
        str(telemetry.production_csv_path or telemetry.output_csv_path or "None"),
    )
    summary_table.add_row(
        "Missing Contact CSV",
        str(telemetry.missing_contact_csv_path or "None"),
    )

    console.print(summary_table)

    if telemetry.rejection_reasons:
        rejection_table = Table(title="Exclusion & Incompletion Breakdown", border_style="dim")
        rejection_table.add_column("Exclusion Reason", style="dim")
        rejection_table.add_column("Count", justify="right", style="red")
        for reason, count in sorted(
            telemetry.rejection_reasons.items(), key=lambda x: x[1], reverse=True
        ):
            rejection_table.add_row(reason, str(count))
        console.print(rejection_table)


def execute_engine_cli(
    year: int | None = None,
    category: str | None = None,
    limit: int = 10000,
    max_source_files: int | None = None,
) -> None:
    """Validate parameters and execute the core production lead engine."""
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    console.print(
        Panel(
            "[bold white]U.S. Contractor Lead-Generation Engine[/bold white]\n"
            "[italic cyan]Florida Sunbiz Corporate Data Ingestion & Multi-Signal Verification[/italic cyan]",
            border_style="blue",
        )
    )

    # 1. Validate & resolve category
    if category is None:
        selected_category = prompt_contractor_category()
        is_interactive = True
    else:
        norm_cat = category.strip().lower().replace("_", "-")
        try:
            get_classifier(norm_cat)
            selected_category = norm_cat
        except ValueError:
            console.print(
                f"[bold red]Error: Unsupported category '{category}'.[/bold red]\n"
                f"Supported categories are: [yellow]{', '.join(SUPPORTED_CATEGORIES)}[/yellow]"
            )
            raise SystemExit(1) from None
        is_interactive = False

    # 2. Validate & resolve year
    if year is not None:
        if year < 1990 or year > 2030:
            console.print(
                f"[bold red]Error: Invalid year {year}. Must be between 1990 and 2030.[/bold red]"
            )
            raise SystemExit(1)
        selected_year = year
    elif is_interactive:
        selected_year = prompt_year()
    else:
        # Flags specified without --year: default to auto multi-year
        selected_year = None

    # 3. Validate limit
    if limit <= 0:
        console.print(
            "[bold red]Error: --limit must be a positive integer greater than 0.[/bold red]"
        )
        raise SystemExit(1)

    if selected_year is not None:
        year_display = f"Year: {selected_year}"
    else:
        year_display = "Auto Multi-Year: 2026 -> 2016"

    console.print(
        f"[green]Starting lead generation engine for[/green] [bold cyan]{selected_category.title()}[/bold cyan] "
        f"[green]({year_display}, Target Limit: {limit:,})[/green]..."
    )

    engine = LeadEngine(concurrency_limit=settings.concurrency_limit)
    try:
        csv_path, telemetry = asyncio.run(
            engine.run(
                year=selected_year,
                category=selected_category,
                limit=limit,
                max_source_files=max_source_files,
            )
        )
        print_final_run_summary(telemetry)

    except KeyboardInterrupt:
        console.print(
            "\n[bold yellow]Execution safely paused by user (Ctrl+C).[/bold yellow]\n"
            "[green]All collected leads have been safely flushed to the data/output/ CSV files and checkpointed.[/green]\n"
            "[cyan]You can resume anytime with the same command without repeating already-saved leads.[/cyan]"
        )
    except Exception as exc:
        console.print(f"[bold red]Execution error: {exc}[/bold red]")
        raise SystemExit(1) from exc


def status_command() -> None:
    """Display system configuration, storage status, and workspace paths."""
    settings = get_settings()

    settings_table = Table(title="System Configuration & Settings", border_style="cyan")
    settings_table.add_column("Setting", style="bold white")
    settings_table.add_column("Value", style="green")

    settings_table.add_row("Concurrency Limit", str(settings.concurrency_limit))
    settings_table.add_row("Request Timeout (s)", str(settings.request_timeout_seconds))
    settings_table.add_row("Retry Count", str(settings.retry_count))
    settings_table.add_row("Rate Limit (req/s)", str(settings.rate_limit_per_second))
    settings_table.add_row("Database Path", str(settings.database_path))
    settings_table.add_row("DuckDB Path", str(settings.duckdb_path))
    settings_table.add_row("Raw Data Dir", str(settings.raw_data_dir))
    settings_table.add_row("Output Dir", str(settings.output_dir))
    settings_table.add_row("Log Level", settings.log_level)

    console.print(settings_table)


def init_db_command() -> None:
    """Initialize persistent database storage and register all schema tables."""
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    console.print("[cyan]Initializing persistent storage...[/cyan]")
    engine = get_engine()
    init_db(engine)

    tables = list(Base.metadata.tables.keys())
    console.print(
        Panel(
            f"[bold green]Database successfully initialized![/bold green]\n"
            f"Location: [cyan]{settings.database_path}[/cyan]\n"
            f"Active Tables ({len(tables)}): [yellow]{', '.join(tables)}[/yellow]",
            title="Storage Initialization",
            border_style="green",
        )
    )


def inspect_runs_command() -> None:
    """View recent pipeline execution runs and checkpoint metrics."""
    init_db_command()
    with get_db_session() as session:
        repo = LeadRepository(session)
        runs = repo.list_recent_runs(limit=10)

        if not runs:
            console.print("[yellow]No execution runs found in database.[/yellow]")
            return

        table = Table(title="Recent Execution Runs & Checkpoints", border_style="magenta")
        table.add_column("Run ID", style="dim", no_wrap=True)
        table.add_column("Category", style="cyan")
        table.add_column("State", style="white")
        table.add_column("Status", style="bold")
        table.add_column("Processed", justify="right")
        table.add_column("Success", justify="right", style="green")
        table.add_column("Failed", justify="right", style="red")
        table.add_column("Started At", style="dim")

        for r in runs:
            status_style = (
                "green" if r.status == "COMPLETED" else "yellow" if r.status == "RUNNING" else "red"
            )
            table.add_row(
                r.id[:8] + "...",
                r.category,
                r.state or "ALL",
                f"[{status_style}]{r.status}[/{status_style}]",
                str(r.processed_records),
                str(r.successful_records),
                str(r.failed_records),
                r.started_at.strftime("%Y-%m-%d %H:%M:%S") if r.started_at else "",
            )

        console.print(table)


def enrich_pest_control_command(
    input_path: str,
    output_path: str,
    limit: int | None = None,
    concurrency: int = 5,
    resume: bool = True,
) -> None:
    """Enrich an existing pest control CSV with verified official websites and trade classification."""
    from contractor_scraper.orchestration.csv_enricher import CSVEnricher

    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    enricher = CSVEnricher(concurrency_limit=concurrency)
    asyncio.run(
        enricher.enrich_csv(
            input_csv_path=input_path,
            output_csv_path=output_path,
            limit=limit,
            resume=resume,
            category="pest control",
        )
    )


def classify_christmas_lights_command(
    input_path: str = "christmas light installation/Christmas Lights Installation.csv",
    output_dir: str = "christmas light installation/output",
    limit: int | None = None,
    concurrency: int = 6,
    resume: bool = True,
    timeout: float = 10.0,
) -> None:
    """Clean and classify Christmas light installation contractor leads from company websites."""
    from contractor_scraper.orchestration.christmas_light_enricher import (
        ChristmasLightLeadEnricher,
    )

    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    enricher = ChristmasLightLeadEnricher(
        concurrency_limit=concurrency,
        timeout_seconds=timeout,
    )
    asyncio.run(
        enricher.process_leads(
            input_csv_path=input_path,
            output_dir=output_dir,
            limit=limit,
            resume=resume,
        )
    )
