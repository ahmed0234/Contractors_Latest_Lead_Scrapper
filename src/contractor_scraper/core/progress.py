"""Rich-based colorful progress display and structured status logging for the lead engine.

Color convention:
  cyan   - current operation, year, or processing stage
  blue   - downloading or reading Sunbiz files
  yellow - searching for websites or emails / warnings
  green  - successfully accepted production-ready lead
  red    - errors, blocks, rate limits, fatal failures
  dim    - debug details, less important information
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# Shared console - import from here to keep a single instance across modules.
console = Console()


def print_engine_banner(category: str, limit: int) -> None:
    console.print(
        Panel(
            f"[bold white]U.S. Contractor Lead-Generation Engine[/bold white]\n"
            f"[italic cyan]Florida Sunbiz Corporate Data Ingestion & Multi-Signal Verification[/italic cyan]\n"
            f"[dim]Category: [bold cyan]{category.title()}[/bold cyan]  |  "
            f"Target Limit: [bold green]{limit:,} production-ready leads[/bold green][/dim]",
            border_style="blue",
        )
    )


def print_year_start(year: int, current_leads: int, target: int) -> None:
    console.print(
        f"\n[bold cyan]=== Processing Year {year} ===[/bold cyan]  "
        f"[dim]({current_leads:,}/{target:,} leads collected so far)[/dim]"
    )


def print_year_complete(year: int, leads_this_year: int, total_leads: int, target: int) -> None:
    console.print(
        f"[cyan]Year {year} complete:[/cyan] "
        f"[green]{leads_this_year:,} new production leads[/green]  |  "
        f"Total: [bold green]{total_leads:,}/{target:,}[/bold green]"
    )


def print_target_reached(total: int, target: int, year: int) -> None:
    console.print(
        Panel(
            f"[bold green]Target Reached![/bold green]  "
            f"{total:,} production-ready leads collected "
            f"(requested {target:,}) -- stopped at year {year}.",
            border_style="green",
        )
    )


def print_years_exhausted(total: int, target: int) -> None:
    console.print(
        Panel(
            f"[bold yellow]All available years exhausted.[/bold yellow]  "
            f"Collected [bold]{total:,}[/bold] of {target:,} requested leads.  "
            f"[dim]No more filing years available.[/dim]",
            border_style="yellow",
        )
    )


def print_file_download(file_name: str, year: int) -> None:
    console.print(f"  [blue]Downloading[/blue] [dim]{file_name}[/dim] [blue](year {year})[/blue]")


def print_file_cached(file_name: str) -> None:
    console.print(f"  [dim]Cached {file_name}[/dim]")


def print_file_processing(file_name: str, file_index: int, total_files: int) -> None:
    console.print(
        f"  [blue]File {file_index}/{total_files}:[/blue] [dim]{file_name}[/dim]"
    )


def print_lead_found(
    lead_num: int,
    company_name: str,
    city: str,
    state: str,
    website: str,
    email: str,
    year: int,
    limit: int,
) -> None:
    email_display = f"  [dim]{email}[/dim]" if email else "  [dim](no email)[/dim]"
    console.print(
        f"  [bold green]Lead #{lead_num:,}[/bold green] "
        f"[green]{company_name}[/green] - {city}, {state} - "
        f"[dim]{website}[/dim]{email_display}  "
        f"[dim cyan]{lead_num}/{limit}[/dim cyan]"
    )


def print_missing_contact(company_name: str, reason: str) -> None:
    console.print(f"  [dim]  Missing contact: {company_name} - {reason}[/dim]")


def print_duplicate_skipped(company_name: str) -> None:
    console.print(f"  [dim]  Duplicate skipped: {company_name}[/dim]")


def print_search_start(provider: str, query: str) -> None:
    short_q = query[:60] + "..." if len(query) > 60 else query
    console.print(f"    [yellow]{provider}:[/yellow] [dim]{short_q}[/dim]")


def print_search_success(provider: str, result_count: int) -> None:
    console.print(f"    [yellow]  -> {provider}:[/yellow] [dim]{result_count} results[/dim]")


def print_search_empty(provider: str) -> None:
    console.print(f"    [dim]  -> {provider}: no results[/dim]")


def print_search_rate_limited(provider: str, status_code: int | None = None) -> None:
    code_str = f" (HTTP {status_code})" if status_code else ""
    console.print(
        f"    [red]  X {provider}: rate-limited / blocked{code_str} -- switching provider[/red]"
    )


def print_search_captcha(provider: str) -> None:
    console.print(
        f"    [red]  X {provider}: CAPTCHA / bot-detection -- switching provider[/red]"
    )


def print_search_error(provider: str, error: str) -> None:
    short_err = error[:80] + "..." if len(error) > 80 else error
    console.print(f"    [red]  X {provider}: error -- {short_err}[/red]")


def print_search_timeout(provider: str) -> None:
    console.print(f"    [yellow]  X {provider}: timeout -- trying next provider[/yellow]")


def print_api_not_configured(api_name: str) -> None:
    console.print(f"    [dim]  {api_name} API key not configured -- skipping[/dim]")


def print_synthetic_probe_result(found: int, total: int) -> None:
    console.print(f"    [dim]  Synthetic DNS probe: {found}/{total} candidates resolved[/dim]")


def print_website_verified(company_name: str, url: str, confidence: float) -> None:
    console.print(
        f"    [green]  Website verified:[/green] [dim]{url}[/dim]  "
        f"[dim](confidence {confidence:.0%})[/dim]"
    )


def print_website_rejected(company_name: str, url: str, reason: str) -> None:
    console.print(f"    [dim]  Website rejected: {url} -- {reason}[/dim]")


def print_email_found(email: str, source: str) -> None:
    console.print(f"    [green]  Email found:[/green] [dim]{email}  ({source})[/dim]")


def print_email_not_found(website: str) -> None:
    console.print(f"    [dim]  No email found on {website}[/dim]")


def print_location_enriched(
    company_name: str,
    original_city: str,
    new_city: str,
    original_state: str,
    new_state: str,
) -> None:
    console.print(
        f"    [dim]  Location enriched: {original_city}, {original_state} "
        f"-> {new_city}, {new_state}  [{company_name}][/dim]"
    )


def print_stats_line(
    raw: int,
    candidates: int,
    websites: int,
    emails: int,
    production: int,
    target: int,
    missing: int,
    duplicates: int,
) -> None:
    t = Text()
    t.append(f"  Raw: {raw:,}", style="dim")
    t.append("  |  ", style="dim")
    t.append(f"Matched: {candidates:,}", style="cyan")
    t.append("  |  ", style="dim")
    t.append(f"Websites: {websites:,}", style="yellow")
    t.append("  |  ", style="dim")
    t.append(f"Emails: {emails:,}", style="yellow")
    t.append("  |  ", style="dim")
    t.append(f"Leads: {production:,}/{target:,}", style="bold green")
    t.append("  |  ", style="dim")
    t.append(f"Missing: {missing:,}", style="dim")
    t.append("  |  ", style="dim")
    t.append(f"Dupes: {duplicates:,}", style="dim")
    console.print(t)
