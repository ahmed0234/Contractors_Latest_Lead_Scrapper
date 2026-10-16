"""Clean and classify Christmas and holiday light installation company leads.

Visits company websites to determine genuine on-site installation service providers
vs. product sellers, wholesale suppliers, unrelated retailers, landscape-only lighting,
and unreachable sites.
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
from contractor_scraper.orchestration.christmas_light_enricher import ChristmasLightLeadEnricher


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
        default="christmas light installation/output",
        help="Directory to save the categorized output CSV lead files.",
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
        default=12,
        help="Maximum concurrent asynchronous website crawl workers.",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=12.0,
        help="Per-website crawl timeout in seconds.",
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

    enricher = ChristmasLightLeadEnricher(
        concurrency_limit=args.concurrency,
        timeout_seconds=args.timeout,
    )
    asyncio.run(
        enricher.process_leads(
            input_csv_path=args.input,
            output_dir=args.output_dir,
            limit=args.limit,
            resume=not args.no_resume,
            retry_unreachable=args.retry_unreachable,
        )
    )


if __name__ == "__main__":
    main()
