# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment & Commands

Windows dev box; virtualenv lives at `.venv` (`.\.venv\Scripts\Activate.ps1`). Python >=3.11. `pip install -r requirements.txt` installs deps plus the package in editable mode (`-e .`); add `pip install -e .[dev]` for pytest/ruff.

```bash
pytest tests/ -v                                            # full suite
pytest tests/test_christmas_light_classifier.py -v          # one file
pytest tests/test_christmas_light_classifier.py::test_name  # one test
pytest -k "christmas and not enricher"                      # by keyword
ruff check src tests          # lint (line-length 100, rules E,W,F,I,B,C4,UP; E501 ignored)
ruff format src tests
```

`pyproject.toml` sets `pythonpath = ["src", "."]` for pytest, so tests import `contractor_scraper` and the root-level scripts directly.

### Entry points
- `python classify_christmas_lights.py [--input X] [--output DIR] [--limit N] [--concurrency 8] [--timeout 20] [--retries 3] [--max-pages 10] [--no-second-pass] [--no-resume] [--no-retry-unreachable]` — Christmas/holiday/permanent-lighting CSV classifier. Default input is auto-detected from `christmas light installation/` (largest CSV, preferring the "Mega Verified" file); outputs default to `christmas light installation/output/<input file stem>/`. That folder is git-ignored working data.
- `python find_christmas_lighting_leads.py [--year Y]... [--download] [--scope holiday|focused|broad] [--limit N] [--max-leads N] [--concurrency 6] [--output CSV] [--no-resume]` — Sunbiz → verified Christmas/holiday lighting installer leads (also `christmas-sunbiz` CLI subcommand). Reads cached Sunbiz files under `data/raw/sunbiz/<year>/` unless `--download`. Output: `data/output/christmas_holiday_lighting/christmas_holiday_lighting_leads.csv` plus `_review.csv`, `_state.jsonl` (resume journal), `_run_summary.json` beside it.
- `python enrich_pest_control.py --input ... --output ...` — pest-control CSV enrichment (`CSVEnricher`).
- `python -m contractor_scraper.cli.app <cmd>` (also `python app.py` / `python scraper.py` / `contractor-scraper` script) — Typer CLI. Subcommands: `run`, `status`, `init-db`, `inspect-runs`, `enrich-pest-control`, `classify-christmas-lights`. Invoking with no subcommand runs the Sunbiz engine (or prompts interactively).
- Root scripts insert `src/` into `sys.path` themselves, so they work without the editable install.

## Configuration
`config/settings.py` is a pydantic-settings `Settings` with env prefix `SCRAPER_` (see `.env.example`), accessed through the `lru_cache`d `get_settings()`. Because it is cached, tests that need different settings must construct `Settings(...)` directly or clear the cache. Optional search API keys (Serper, Google CSE, SerpApi) are used by `WebsiteFinder` when present; otherwise it falls back to public DuckDuckGo/Yahoo scraping, directory-profile outbound links, and synthetic-domain DNS probing.

## Architecture

There are two largely independent pipelines that share the discovery/crawl layer:

### 1. CSV classification/enrichment pipelines (most active work)
`orchestration/christmas_light_enricher.py` (`ChristmasLightLeadEnricher.process_leads`) and `orchestration/csv_enricher.py` (pest control):
1. Read the input CSV with the stdlib `csv` module, **preserving every original column and row verbatim** (multi-line fields included). Column positions (website, name, etc.) are detected heuristically from headers in `_identify_columns`.
2. Crawl each website with `discovery/crawler.py::WebsiteCrawler` (httpx, Chrome-like headers, http/https and www variants, bot-challenge / parked-domain / DNS-failure detection, link scoring via `score_link`, on-disk cache under `data/cache/<CrawlerConfig.cache_namespace>`; only complete, accessible crawls are reused from cache). Behaviour is controlled by `CrawlerConfig`: its **defaults reproduce the original lightweight crawl** used by the Sunbiz engine and pest-control pipeline; the Christmas pipeline opts into `christmas_crawler_config()` (retries with exponential backoff/`Retry-After`, per-domain request gate + delay, robots.txt respected, sitemap discovery, two-level deep links, holiday-path probes, soft-404 filtering, no Googlebot impersonation). A domain crawl that exceeds its time budget returns partial results with `incomplete_reason` set.
3. Classify the `CrawlResult` with a dedicated classifier — `classification/christmas_light_classifier.py::ChristmasLightClassifier.evaluate_crawl` or `classification/pest_control_service_classifier.py`. The Christmas classifier grades evidence **per page and in context**: explicit installation phrases/patterns are `strong`; generic terms ("holiday lighting") only become `moderate` on a dedicated page or in a service context on a service business; matches that are negated, in product listings, or in DIY/safety-tip phrasing are downgraded to `weak`. Yes requires strong/moderate evidence; meaningful weak-only evidence → Uncertain; exclusion categories (landscape-only, indoor electrical, event, security, retail, attractions, `KNOWN_NON_CONTRACTOR_DOMAINS`) → No. It also emits per-service fields (Christmas / Holiday / Permanent decorative: Yes/No/Unclear), evidence URLs, and keywords.
4. Christmas outputs: `qualified_leads.csv`, `not_qualified_leads.csv`, `uncertain_leads.csv`, `unreachable_leads.csv`, `all_leads.csv` (original columns + `ENRICHMENT_HEADERS`; `all_leads.csv` alone also gets a trailing `is our niche company ?` column = yes/no/uncertain/unreachable), plus `crawl_checkpoint.jsonl` and `run_report.json` (totals, QA validation, persistent failures). Exact duplicate input rows are dropped; rows sharing a domain are crawled once.
5. Resume: the JSONL checkpoint is keyed by the input file's SHA-256, so it only restores results for the same input; Unreachable/incomplete results are re-evaluated by default. After the main pass, a "patient" second pass re-crawls temporary failures with longer timeouts. Live sites that are bot-challenged or temporarily failing fall back to a Wayback Machine snapshot (`WebsiteCrawler.crawl_archived`, globally throttled by `ArchiveThrottle` — the archive refuses connections if hit too fast); archived verdicts are confidence-discounted and snapshots older than `MAX_ARCHIVE_AGE_YEARS` become Uncertain. Archive fallback and the second pass only use real `WebsiteCrawler` instances, never test subclasses.

Tuning classification accuracy almost always means editing the phrase lists/rules in the classifier module and adding cases to `tests/test_christmas_light_*.py` (`test_christmas_light_engine_v2.py` covers crawler behaviour via `httpx.MockTransport`); crawler changes (link scoring, recovery, discovery) live in `crawler.py`.

### 1b. Sunbiz → Christmas/holiday lighting engine
`orchestration/christmas_sunbiz_engine.py::ChristmasSunbizLeadEngine.run`: scans Sunbiz files in a thread, pre-filters names with `classification/christmas_name_filter.py` (tiers: niche name → lighting → adjacent trades with `--scope broad`), and feeds a priority queue to N workers. Each worker: `discovery/christmas_website_finder.py` (subclass of `WebsiteFinder` with niche queries, a shared `SearchGate`, longer `search_timeout`, and `search_responded` so blocked search is retryable, not "no website") → crawl with `christmas_crawler_config` (own cache namespace) → `discovery/company_identity.py::verify_company_identity` (strict; niche/legal words never identify a company, name coverage uses homepage title + domain only) → `ChristmasLightClassifier` (lead only if Yes with Christmas/holiday/permanent-holiday service; Yes-below-threshold/Uncertain → review CSV) → email from the verified site only (own domain or free-mail; MX/A check via DoH in `extraction/mail_domain.py`). Each lead is appended + fsynced immediately (`IncrementalCsvWriter` holds rows in memory if Excel locks the file), then journaled; retryable statuses (`search_unavailable`, `website_unreachable`, `timeout`, `error`) are retried on later runs up to `--max-attempts`. Tests: `tests/test_christmas_sunbiz_engine.py`.

### 2. Sunbiz state-filing lead engine
`orchestration/lead_engine.py::LeadEngine.run`: `acquisition/sunbiz.py` streams Florida Sunbiz fixed-width (1440-char) records → `parsing/sunbiz_parser.py` → `normalization/` → category filter via `classification/classifier.py` (`get_classifier(category)`; one `BaseCategoryClassifier` subclass per `ContractorCategory` with strong/supporting/negative terms and exclusion regexes) → `discovery/website_finder.py` → `website_verifier.py` → crawler → `extraction/email_extractor.py` + `email_verifier.py` → `matching/dedup.py` (fingerprint dedup across years) → `exporting/exporter.py`.
- With no `--year`, it walks backward from the latest year to `MIN_YEAR` (2016) until `limit` complete leads are produced.
- A lead only counts toward the limit if it has **both** a verified website and an email; website-only leads go to a separate missing-contact export.
- Run/attempt state is persisted in SQLite (SQLAlchemy, WAL mode) via `storage/repository.py`; schema in `storage/models.py` with Alembic migrations in `migrations/`. `storage/duckdb_engine.py` attaches the SQLite DB for analytics/Parquet export.
- `orchestration/pipeline.py` + `stage.py` define a generic stage abstraction that the engine does not fully rely on.

### Shared conventions
- Async I/O everywhere (httpx + `asyncio` semaphores sized by `concurrency`).
- Terminal output uses `rich` (`core/progress.py`, telemetry `print_summary` classes); logging via `core/logging.py::get_logger`, optionally JSON to `logs/`.
- Tests are fully offline: network is mocked, and `tests/conftest.py` provides `tmp_workspace`, `test_settings` (paths pointed at tmp dirs), in-memory SQLite `test_engine`/`test_session`, and `make_sunbiz_line()` for fixed-width records.
- `backup/`, `scratch/`, `input_files/`, and `christmas light installation/` are local data/scratch folders, not source.
