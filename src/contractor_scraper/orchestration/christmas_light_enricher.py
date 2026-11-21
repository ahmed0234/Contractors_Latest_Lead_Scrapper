"""Production-grade Christmas, Holiday & Permanent Decorative Light Installation lead classification pipeline.

Reads contractor leads from CSV, crawls each company website thoroughly (homepage, navigation,
sitemap, holiday/permanent-lighting service pages), classifies genuine installation service
providers vs. product sellers and unrelated trades, and writes four segregated output CSVs with
enrichment columns. 100% preserves original input data, columns, multi-line values, and row order.

Reading, resume checkpoints, concurrency, the patient second pass, CSV output, and QA are shared
with other niches in `niche_enricher.NicheLeadEnricher`; rows sharing a domain share one verdict.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from contractor_scraper.classification.christmas_light_classifier import (
    CLASSIFIER_VERSION,
    ChristmasLightClassificationResult,
    ChristmasLightClassifier,
)
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.crawler import (
    TRANSIENT_STATUS_CATEGORIES,
    CrawlerConfig,
    CrawlResult,
    WebsiteCrawler,
)
from contractor_scraper.orchestration.niche_enricher import (
    ALL_LEADS_FILENAME,
    ARCHIVE_FALLBACK_CATEGORIES,
    CHECKPOINT_FILENAME,
    MAX_ARCHIVE_AGE_YEARS,
    NICHE_COLUMN,
    NICHE_VALUES,
    OUTPUT_FILENAMES,
    PROFILE_PLATFORM_HOSTS,
    REPORT_FILENAME,
    ColumnMap,
    EnrichmentLead,
    NicheEnrichmentTelemetry,
    NicheLeadEnricher,
)

__all__ = [
    "ALL_LEADS_FILENAME",
    "ARCHIVE_FALLBACK_CATEGORIES",
    "CHECKPOINT_FILENAME",
    "ENRICHMENT_HEADERS",
    "HOLIDAY_PRIORITY_KEYWORDS",
    "MAX_ARCHIVE_AGE_YEARS",
    "NICHE_COLUMN",
    "NICHE_VALUES",
    "OUTPUT_FILENAMES",
    "PROFILE_PLATFORM_HOSTS",
    "REPORT_FILENAME",
    "ChristmasEnrichmentTelemetry",
    "ChristmasLightLeadEnricher",
    "christmas_crawler_config",
]

logger = get_logger("christmas_light_enricher")

HOLIDAY_PRIORITY_KEYWORDS: list[str] = [
    "/christmas",
    "/holiday",
    "/holiday-lighting",
    "/christmas-lights",
    "/permanent-lighting",
    "/seasonal-lighting",
    "/lighting",
    "/services",
    "/outdoor-lighting",
    "/exterior-lighting",
    "/residential",
    "/commercial",
    "/gallery",
    "/faq",
    "/about",
    "/contact",
    "/quote",
    "/estimate",
]

ENRICHMENT_HEADERS: list[str] = [
    "Qualification_Status",  # Qualified | Rejected | Needs Review | Unverified
    "Qualified_Niches",
    "Christmas_Light_Installation",
    "Holiday_Light_Installation",
    "Permanent_Decorative_Lighting_Installation",
    "Roofline_Lighting_Installation",
    "Evidence_URLs",
    "Evidence_Summary",
    "Service_Keywords_Found",
    "Crawl_Status",
    "Classification_Confidence",
]


def christmas_crawler_config(read_timeout: float = 20.0) -> CrawlerConfig:
    """Patient, thorough crawl profile for holiday-lighting qualification."""
    read_timeout = max(5.0, read_timeout)
    return CrawlerConfig(
        connect_timeout=min(15.0, max(8.0, read_timeout * 0.6)),
        read_timeout=read_timeout,
        domain_timeout=max(120.0, read_timeout * 7.0),
        max_retries=3,
        backoff_base=2.0,
        backoff_max=20.0,
        per_domain_concurrency=3,
        request_delay=0.35,
        max_deep_pages=6,
        max_probe_pages=6,
        respect_robots_txt=True,
        use_sitemap=True,
        max_sitemap_pages=5,
        impersonate_search_bot_on_challenge=False,
        render_javascript=True,
        block_separators=True,
        # v3: page text now keeps block separators, so v2 cache entries are not comparable
        cache_namespace="crawler_christmas_v3",
    )


@dataclass
class ChristmasEnrichmentTelemetry(NicheEnrichmentTelemetry):
    """Execution telemetry and metrics for Christmas Light Installation lead classification."""

    summary_title: str = "Christmas Light Installation Lead Classification Summary"


class ChristmasLightLeadEnricher(NicheLeadEnricher[ChristmasLightClassificationResult]):
    """Manages reading, crawling, classifying, checkpointing, and saving Christmas light contractor leads."""

    pipeline_title = "Christmas / Holiday / Permanent Lighting Lead Classification Engine"
    summary_title = "Christmas Light Installation Lead Classification Summary"
    classifier_version = CLASSIFIER_VERSION
    enrichment_headers = ENRICHMENT_HEADERS
    shares_result_per_site = True
    telemetry_cls = ChristmasEnrichmentTelemetry

    def __init__(
        self,
        classifier: ChristmasLightClassifier | None = None,
        crawler: WebsiteCrawler | None = None,
        concurrency_limit: int = 8,
        timeout_seconds: float = 20.0,
        crawler_config: CrawlerConfig | None = None,
        max_subpages: int = 10,
        archive_fallback: bool = True,
    ) -> None:
        self.classifier = classifier or ChristmasLightClassifier()
        self.crawler_config = crawler_config or christmas_crawler_config(timeout_seconds)
        self.max_subpages = max_subpages
        super().__init__(
            crawler=crawler
            or WebsiteCrawler(
                timeout_seconds=timeout_seconds,
                max_subpages=max_subpages,
                priority_keywords=HOLIDAY_PRIORITY_KEYWORDS,
                config=self.crawler_config,
            ),
            concurrency_limit=concurrency_limit,
            archive_fallback=archive_fallback,
        )

    # ------------------------------------------------------------------ input handling
    def _identify_columns(
        self, headers: list[str]
    ) -> tuple[int, int, int | None, list[str], int | None, int | None]:
        """Identify key column indices while preserving existing header order.

        Returns:
            (clean_web_idx, company_idx, existing_class_idx, final_headers, raw_web_idx, alt_name_idx)
        """
        header_lower = [h.strip().lower() for h in headers]

        # 1. Locate Clean Website URL
        clean_web_idx = None
        for idx, h in enumerate(header_lower):
            if any(
                p in h
                for p in [
                    "clean website url",
                    "clean website",
                    "clean_website",
                    "clean url",
                    "website url",
                ]
            ):
                clean_web_idx = idx
                break

        # 2. Locate Raw Website URL (often contains specific branch/location landing page)
        raw_web_idx = None
        for idx, h in enumerate(header_lower):
            if idx != clean_web_idx and h in ("website", "url", "domain"):
                raw_web_idx = idx
                break

        if clean_web_idx is None:
            clean_web_idx = raw_web_idx if raw_web_idx is not None else 8

        # 3. Locate Company Name
        company_idx = None
        alt_name_idx = None
        for idx, h in enumerate(header_lower):
            if any(
                p in h
                for p in [
                    "company name",
                    "company_name",
                    "business name",
                    "business_name",
                    "contractor_name",
                ]
            ):
                company_idx = idx
                break

        for idx, h in enumerate(header_lower):
            if idx != company_idx and h in ("name", "company"):
                alt_name_idx = idx
                break

        if company_idx is None:
            company_idx = alt_name_idx if alt_name_idx is not None else 0

        # 4. Check for existing classification column
        class_idx = None
        for idx, h in enumerate(header_lower):
            if any(
                p in h
                for p in [
                    "does this company provide christmas light installation",
                    "does provide christmas light installation service",
                    "does provide christmas light installation",
                    "is christmas light installation",
                ]
            ):
                class_idx = idx
                break

        final_headers = list(headers)
        return (
            clean_web_idx,
            company_idx,
            class_idx,
            final_headers,
            raw_web_idx,
            alt_name_idx,
        )

    def _column_map(self, headers: list[str]) -> ColumnMap:
        web_idx, name_idx, class_idx, _, raw_web_idx, alt_name_idx = self._identify_columns(headers)
        return ColumnMap(
            website=web_idx,
            company=name_idx,
            existing_class=class_idx,
            raw_website=raw_web_idx,
            alt_name=alt_name_idx,
        )

    # ------------------------------------------------------------------ crawling
    async def _evaluate(
        self, crawler: WebsiteCrawler, url: str, company: str
    ) -> ChristmasLightClassificationResult:
        try:
            crawl_res: CrawlResult = await crawler.crawl_domain(url)
            result = self.classifier.evaluate_crawl(crawl_res, company_name=company)
            if (
                result.classification == "Unreachable"
                and self.archive_fallback
                and type(crawler) is WebsiteCrawler
                and crawl_res.status_category in ARCHIVE_FALLBACK_CATEGORIES
            ):
                archived = await crawler.crawl_archived(url)
                if archived is not None:
                    return self._classify_archived(archived, crawl_res, result, company)
            return result
        except Exception as exc:
            logger.debug(f"Error evaluating {url}: {exc}")
            return ChristmasLightClassificationResult(
                classification="Unreachable",
                decision_label="Uncertain",
                confidence=0.0,
                website_status="Connection Error",
                reasoning=f"Unexpected error visiting {url}: {exc}",
                access_error=str(exc),
            )

    def _classify_archived(
        self,
        archived: CrawlResult,
        live: CrawlResult,
        live_result: ChristmasLightClassificationResult,
        company: str,
    ) -> ChristmasLightClassificationResult:
        """Classify an archived snapshot, discounting confidence and refusing to decide on stale snapshots."""
        res = self.classifier.evaluate_crawl(archived, company_name=company)
        ts = archived.archived_snapshot
        snap_date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else "unknown date"
        age_years = datetime.now().year - int(ts[:4]) if ts[:4].isdigit() else 99
        prefix = (
            f"Live site unavailable ({live.status_category}: {(live.error_message or '')[:120]}); "
            f"evaluated Wayback Machine snapshot from {snap_date}. "
        )
        res.website_status = f"Archived Copy (live: {live.status_category})"
        res.access_error = live_result.access_error
        res.confidence = round(res.confidence * 0.9, 3)

        if res.classification in ("Yes", "No") and age_years > MAX_ARCHIVE_AGE_YEARS:
            verdict = "showed" if res.classification == "Yes" else "did not show"
            res.classification = "Uncertain"
            res.decision_label = "Uncertain"
            res.confidence = 0.35
            res.reasoning = (
                f"{prefix}The most recent usable snapshot is over {MAX_ARCHIVE_AGE_YEARS} years old and {verdict} "
                f"qualifying services; current offering needs verification. Archived finding: {res.reasoning}"
            )
            return res
        if res.classification == "Unreachable":
            return live_result
        res.reasoning = prefix + res.reasoning
        return res

    @staticmethod
    def _needs_retry(res: ChristmasLightClassificationResult) -> bool:
        if res.website_status.startswith("Archived Copy"):
            return any(f"live: {cat}" in res.website_status for cat in TRANSIENT_STATUS_CATEGORIES)
        if res.classification == "Unreachable":
            return res.website_status in TRANSIENT_STATUS_CATEGORIES
        if res.classification == "Uncertain":
            return "incomplete" in (res.crawl_details or "") or "could not be evaluated" in res.reasoning
        return False

    async def _evaluate_group(
        self, crawler: WebsiteCrawler, leads: list[EnrichmentLead]
    ) -> dict[int, ChristmasLightClassificationResult]:
        res = await self._evaluate(crawler, leads[0].crawl_url, leads[0].company)
        return {lead.index: res for lead in leads}

    def _result_from_dict(self, data: dict[str, Any]) -> ChristmasLightClassificationResult:
        return ChristmasLightClassificationResult.from_dict(data)

    def _missing_url_result(self, lead: EnrichmentLead) -> ChristmasLightClassificationResult:
        return ChristmasLightClassificationResult(
            classification="Uncertain",
            decision_label="Uncertain",
            confidence=0.0,
            website_status="Missing URL",
            reasoning="Website URL is empty or invalid in the input record; cannot verify services.",
        )

    # ------------------------------------------------------------------ output
    @staticmethod
    def _crawl_status(res: ChristmasLightClassificationResult) -> str:
        status = res.website_status or ""
        if res.crawl_details:
            status = f"{status} | {res.crawl_details}"
        if res.classification == "Unreachable" and res.access_error:
            status = f"{status} | {res.access_error[:300]}"
        return status

    def _enrichment_values(self, res: ChristmasLightClassificationResult | None) -> list[str]:
        if res is None:
            return ["Pending"] + [""] * (len(ENRICHMENT_HEADERS) - 1)
        return [
            res.qualification_status,
            "; ".join(res.qualified_niches),
            res.christmas_service,
            res.holiday_service,
            res.permanent_service,
            res.roofline_service,
            " | ".join(res.evidence_urls),
            res.reasoning,
            "; ".join(res.keywords_found or res.evidence_phrases),
            self._crawl_status(res),
            f"{res.confidence:.2f}",
        ]

    # ------------------------------------------------------------------ QA
    def _has_evidence(self, res: ChristmasLightClassificationResult) -> bool:
        return bool(res.evidence_urls and res.reasoning)

    def _extra_validation(self, leads: list[EnrichmentLead], lead_result: Any) -> dict[str, Any]:
        single_page_rejections = [
            lead.company
            for lead in leads
            if (r := lead_result(lead)) is not None
            and r.classification == "No"
            and r.pages_evaluated <= 1
            and "national retail" not in r.reasoning
        ]
        return {"not_qualified_single_page_decisions": len(single_page_rejections)}
