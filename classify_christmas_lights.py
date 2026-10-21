"""Clean and classify Christmas, holiday, and permanent decorative light installation company leads.

Thoroughly crawls each company website (navigation, sitemap, holiday/permanent lighting pages) to
determine genuine installation service providers vs. product sellers, wholesale suppliers, unrelated
retailers, landscape-only lighting, general electricians, and unreachable sites.

Outputs (inside the Christmas Light Installation output folder):
    qualified_leads.csv, not_qualified_leads.csv, uncertain_leads.csv, unreachable_leads.csv,
    all_leads.csv, crawl_checkpoint.jsonl (resume state), run_report.json (QA + summary)
"""

import argparse
import asyncio
from pathlib import Path
import sys

# Ensure src/ directory is on Python search path
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import setup_logging
from contractor_scraper.orchestration.christmas_light_enricher import (
    ChristmasLightLeadEnricher,
    christmas_crawler_config,
)


def resolve_default_input_file() -> str:
    """Resolve the default input CSV file dynamically from the christmas light installation folder."""
    base_folder = Path("christmas light installation")
    mega_file = base_folder / "Christmas Lights Installation Mega Verified New Set of Leads.csv"
    if mega_file.exists():
        return str(mega_file)
    if base_folder.exists():
        csv_files = [f for f in base_folder.glob("*.csv") if not f.name.startswith(".")]
        if csv_files:
            csv_files.sort(key=lambda p: p.stat().st_size, reverse=True)
            return str(csv_files[0])
    return "christmas light installation/Christmas Lights Installation.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean and classify Christmas light installation contractor leads from company websites.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default=resolve_default_input_file(),
        help="Path to input CSV containing Christmas light contractor records.",
    )
    parser.add_argument(
        "--output-dir",
        "--output",
        "-o",
        type=str,
        default=None,
        help="Directory for output CSV files (default: christmas light installation/output/<input file name>).",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum records to process (useful for sampling or test batches).",
    )
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
        "--retries",
        type=int,
        default=3,
        help="Retries with exponential backoff for temporary failures (timeouts, 429, 5xx).",
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
    parser.add_argument(
        "--no-resume",
        action="store_true",
        default=False,
        help="Disable automatic resume from already-processed output file.",
    )
    parser.add_argument(
        "--retry-unreachable",
        action="store_true",
        default=True,
        help="When resuming, re-evaluate records previously marked Unreachable or Uncertain using enhanced recovery.",
    )
    parser.add_argument(
        "--no-retry-unreachable",
        action="store_false",
        dest="retry_unreachable",
        help="Do not re-evaluate unreachable records when resuming.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    config = christmas_crawler_config(args.timeout)
    config.max_retries = max(0, args.retries)
    enricher = ChristmasLightLeadEnricher(
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
