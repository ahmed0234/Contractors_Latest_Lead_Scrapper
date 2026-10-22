"""Discover Christmas & holiday lighting installation companies from Florida Sunbiz filings.

Scans Sunbiz daily corporate filing files (local cache under data/raw/sunbiz by default), researches
niche-relevant registrations, verifies each company's official website, confirms Christmas / holiday
light installation from the site content, finds a publicly listed email, and appends every verified
lead to the CSV immediately. Re-running the same command resumes where the last run stopped.

Default output: data/output/christmas_holiday_lighting/christmas_holiday_lighting_leads.csv
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure src/ directory is on Python search path
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from contractor_scraper.classification.christmas_name_filter import (  # noqa: E402
    SCOPE_BROAD,
    SCOPE_FOCUSED,
    SCOPE_HOLIDAY,
)
from contractor_scraper.config.settings import get_settings  # noqa: E402
from contractor_scraper.core.logging import setup_logging  # noqa: E402
from contractor_scraper.orchestration.christmas_sunbiz_engine import (  # noqa: E402
    ChristmasSunbizConfig,
    ChristmasSunbizLeadEngine,
    print_engine_summary,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find verified Christmas / holiday lighting installation companies from Sunbiz filings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Leads CSV path (review CSV, state journal and run summary are written next to it). "
        "Default: data/output/christmas_holiday_lighting/christmas_holiday_lighting_leads.csv",
    )
    parser.add_argument(
        "--year", "-y", type=int, action="append", default=None,
        help="Sunbiz filing year to scan (repeatable). Default: every year cached under data/raw/sunbiz, newest first.",
    )
    parser.add_argument(
        "--download", action="store_true", default=False,
        help="Fetch missing daily filing files from the Sunbiz portal (about 1 GB per year). Default: local cache only.",
    )
    parser.add_argument("--max-files", type=int, default=None, help="Maximum daily filing files per year to scan.")
    parser.add_argument(
        "--scope", choices=[SCOPE_HOLIDAY, SCOPE_FOCUSED, SCOPE_BROAD], default=SCOPE_FOCUSED,
        help="Which registrations to research: 'holiday' = niche names only; 'focused' = plus lighting businesses; "
        "'broad' = plus adjacent trades (pressure washing, gutters, landscaping, roofing, electrical) - much slower.",
    )
    parser.add_argument("--include-inactive", action="store_true", default=False, help="Also research inactive registrations.")
    parser.add_argument("--limit", "-l", type=int, default=None, help="Maximum registrations to research this run (for testing).")
    parser.add_argument("--max-leads", type=int, default=None, help="Stop after this many new qualified leads.")
    parser.add_argument("--concurrency", "-c", type=int, default=6, help="Registrations researched in parallel.")
    parser.add_argument("--timeout", "-t", type=float, default=15.0, help="Per-request read timeout (seconds) for website crawls.")
    parser.add_argument(
        "--candidate-timeout", type=float, default=420.0,
        help="Total research budget per registration (search + up to --max-sites site crawls).",
    )
    parser.add_argument("--max-sites", type=int, default=3, help="Candidate websites crawled per registration.")
    parser.add_argument(
        "--search-interval", type=float, default=1.5,
        help="Minimum seconds between public search-engine requests (shared by all workers).",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.0,
        help="Minimum classifier confidence for an automatic lead; lower 'Yes' verdicts go to the review CSV.",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=3,
        help="Runs in which a registration with a temporary failure (blocked search, unreachable site) is retried.",
    )
    parser.add_argument(
        "--no-resume", action="store_true", default=False,
        help="Ignore the state journal and re-research everything (saved leads are still never duplicated).",
    )
    parser.add_argument("--no-mail-check", action="store_true", default=False, help="Skip the MX/A check on email domains.")
    parser.add_argument(
        "--log-level", type=str, default=None, help="Logging verbosity (DEBUG, INFO, WARNING...). Default: SCRAPER_LOG_LEVEL."
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> ChristmasSunbizConfig:
    return ChristmasSunbizConfig(
        output_csv=Path(args.output) if args.output else None,
        years=args.year,
        local_only=not args.download,
        max_files=args.max_files,
        scope=args.scope,
        include_inactive=args.include_inactive,
        limit=args.limit,
        max_leads=args.max_leads,
        concurrency=max(1, args.concurrency),
        timeout=args.timeout,
        candidate_timeout=args.candidate_timeout,
        max_candidate_sites=max(1, args.max_sites),
        search_interval=max(0.0, args.search_interval),
        min_classification_confidence=args.min_confidence,
        resume=not args.no_resume,
        max_attempts=max(1, args.max_attempts),
        check_mail_domains=not args.no_mail_check,
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=args.log_level or settings.log_level, logs_dir=settings.logs_dir)
    if (args.log_level or settings.log_level).upper() != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per HTTP request drowns the lead log

    engine = ChristmasSunbizLeadEngine(config=build_config(args))
    try:
        summary = asyncio.run(engine.run())
    except KeyboardInterrupt:
        print(
            "\nInterrupted. Every lead verified so far is already saved in "
            f"{engine.output_csv.resolve()} - run the same command again to resume."
        )
        return
    print_engine_summary(summary)


if __name__ == "__main__":
    main()
