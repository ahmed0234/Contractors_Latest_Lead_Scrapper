"""Enrich existing Pest Control CSV lead dataset with official websites and service qualifications.

Usage example:
    python enrich_pest_control.py --input "input_files/Pest Control - James  - Final Raw Leads.csv" --output "output_files/enriched_pest_control_leads.csv"
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
from contractor_scraper.orchestration.csv_enricher import CSVEnricher


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enrich Pest Control CSV with verified official websites and trade classification.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default="input_files/Pest Control - James  - Final Raw Leads.csv",
        help="Path to input CSV containing pest control companies.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="data/output/enriched_pest_control_leads.csv",
        help="Path to save enriched output CSV file.",
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
        default=5,
        help="Maximum concurrent asynchronous website discovery & crawl workers.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        default=False,
        help="Disable automatic resume from already-processed output file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    enricher = CSVEnricher(concurrency_limit=args.concurrency)
    asyncio.run(
        enricher.enrich_csv(
            input_csv_path=args.input,
            output_csv_path=args.output,
            limit=args.limit,
            resume=not args.no_resume,
            category="pest control",
        )
    )


if __name__ == "__main__":
    main()
