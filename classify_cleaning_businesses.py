"""Cleaning Business Enrichment & Classification: is each business a residential cleaning company, which
cleaning services does its website advertise, and which one is its highest-value (main) service?

For every row it crawls the business website (homepage, services pages, dedicated service pages, about /
contact), then, in this order:

1. decides whether the business is a cleaning company (directories, product sellers, software, commercial-only
   janitorial companies, and unrelated businesses are "No"),
2. for cleaning companies, extracts the services the website actually advertises (blog posts, tips, product
   listings, negated mentions, and maid-checklist tasks do not count),
3. picks the ONE highest-value residential service for the "Main Service" column.

The "Is Cleaning Company?" column always receives Yes or No; the services go to a "Services" column. All
original columns and rows are preserved.

Outputs (default: <cleaning folder>/output/<input file name>/):
    qualified_leads.csv, not_qualified_leads.csv, uncertain_leads.csv, unreachable_leads.csv,
    all_leads.csv, duplicates.csv (rows sharing Business Name AND email), classification_report.csv
    (one-line reason per row + Borderline flag), crawl_checkpoint.jsonl (resume state), run_report.json
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure src/ directory is on Python search path
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from contractor_scraper.config.settings import get_settings  # noqa: E402
from contractor_scraper.core.logging import setup_logging  # noqa: E402
from contractor_scraper.orchestration.cleaning_enricher import (  # noqa: E402
    CleaningLeadEnricher,
    cleaning_crawler_config,
)

INPUT_FOLDERS = ("Cleaning_Mode", "cleaning mode", "Cleaning Mode", "cleaning_mode")


def resolve_default_input_file() -> str:
    """Largest CSV in the cleaning folder (its input/ subfolder first)."""
    for name in INPUT_FOLDERS:
        base = Path(name)
        for folder in (base / "input", base):
            if folder.is_dir():
                csv_files = [f for f in folder.glob("*.csv") if not f.name.startswith(".")]
                if csv_files:
                    csv_files.sort(key=lambda p: p.stat().st_size, reverse=True)
                    return str(csv_files[0])
    return "Cleaning_Mode/Cleaning Businesses.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify cleaning businesses (Yes/No + Services + Main Service) from their websites.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", type=str, default=resolve_default_input_file(), help="Path to the input CSV.")
    parser.add_argument(
        "--output-dir",
        "--output",
        "-o",
        type=str,
        default=None,
        help="Directory for output CSV files (default: <cleaning folder>/output/<input file name>).",
    )
    parser.add_argument("--limit", "-l", type=int, default=None, help="Maximum records to process (test batches).")
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=8,
        help="Maximum websites crawled concurrently (each site also has its own per-domain request limit).",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=20.0,
        help="Per-request read timeout in seconds (connect and whole-site budgets scale from it).",
    )
    parser.add_argument(
        "--retries", type=int, default=3, help="Retries with exponential backoff for temporary failures (timeouts, 429, 5xx)."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Maximum navigation subpages per site (sitemap, deep-link, and probe pages are added on top).",
    )
    parser.add_argument(
        "--no-second-pass",
        action="store_true",
        default=False,
        help="Skip the slower second pass that re-crawls websites with temporary failures.",
    )
    parser.add_argument("--no-resume", action="store_true", default=False, help="Start over instead of resuming.")
    parser.add_argument(
        "--retry-unreachable",
        action="store_true",
        default=True,
        help="When resuming, re-crawl websites that previously failed with temporary errors.",
    )
    parser.add_argument(
        "--no-retry-unreachable",
        action="store_false",
        dest="retry_unreachable",
        help="Do not re-crawl previously failed websites when resuming.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    config = cleaning_crawler_config(args.timeout)
    config.max_retries = max(0, args.retries)
    enricher = CleaningLeadEnricher(
        concurrency_limit=args.concurrency,
        timeout_seconds=args.timeout,
        crawler_config=config,
        max_subpages=args.max_pages,
    )
    asyncio.run(
        enricher.process_leads(
            input_csv_path=args.input,
            output_dir=args.output_dir,
            limit=args.limit,
            resume=not args.no_resume,
            retry_unreachable=args.retry_unreachable,
            second_pass=not args.no_second_pass,
        )
    )


if __name__ == "__main__":
    main()
