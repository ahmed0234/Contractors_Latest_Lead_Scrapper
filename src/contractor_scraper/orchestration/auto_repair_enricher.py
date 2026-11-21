"""Auto repair lead classification pipeline: does each business provide vehicle repair / maintenance?

Same architecture, CLI style, concurrency, retries, caching, checkpoint/resume, second pass, archive
fallback, and output structure as the Christmas lighting pipeline (shared via `NicheLeadEnricher`).
Differences that matter for this niche:

- The crawl uses an auto-repair link profile (services / repair / collision / tires / about /
  contact pages first; inventory pages to recognise dealerships) and never short-circuits national
  retail domains (Walmart / Mavis auto centers repair cars).
- The website on each row is accepted as the business's own site (no ownership verification). One
  domain is crawled once, but every row is classified separately because the row's name still
  counts: one email domain (a parent company, accountant, or tire manufacturer) can carry hundreds of
  differently-named businesses.
- Duplicates (same Business_Name AND same Email, case/punctuation-insensitive) are classified once and
  the verdict is copied to every row of the group; all rows of every group, first occurrence
  included, are also written to duplicates.csv. qualified_leads.csv lists each duplicate group only
  once (its first occurrence); the other sheets keep every row.
- The "Is Auto Repair ?" column always receives Yes or No; low-confidence verdicts are additionally
  routed to uncertain_leads.csv / unreachable_leads.csv for manual review, and
  classification_report.csv logs a one-line reason per row with a Borderline flag.
"""

import re
from typing import Any

from contractor_scraper.classification.auto_repair_classifier import (
    CLASSIFIER_VERSION,
    REVIEW_THRESHOLD,
    AutoRepairClassificationResult,
    AutoRepairClassifier,
    LeadInfo,
    analyze_name,
    clean_dba,
    downgrade_unusable,
    profile_site,
)
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.crawler import (
    TRANSIENT_STATUS_CATEGORIES,
    CrawlerConfig,
    CrawlResult,
    LinkProfile,
    WebsiteCrawler,
)
from contractor_scraper.orchestration.niche_enricher import (
    ARCHIVE_FALLBACK_CATEGORIES,
    ColumnMap,
    EnrichmentLead,
    NicheLeadEnricher,
    find_column,
)

logger = get_logger("auto_repair_enricher")

AUTO_REPAIR_LINK_PROFILE = LinkProfile(
    name="auto_repair",
    primary_terms=(
        "repair", "repairs", "service", "services", "maintenance", "collision", "body-shop", "body shop",
        "auto-body", "auto body", "brake", "brakes", "tire", "tires", "alignment", "transmission",
        "oil-change", "oil change", "diagnostic", "mechanic", "fleet", "inspection", "muffler", "exhaust",
        "glass", "windshield", "ac-repair", "air-conditioning", "engine", "what-we-do", "what we do",
    ),
    secondary_terms=(
        "about", "contact", "location", "locations", "directions", "hours", "our-story", "our story",
        "who-we-are", "who we are", "find-us", "visit",
    ),
    # Dealership / excluded-trade pages: worth one look to recognise non-repair businesses
    service_terms=(
        "inventory", "new-vehicles", "new vehicles", "used-vehicles", "used vehicles", "pre-owned",
        "specials", "coupons", "financing", "towing", "detailing", "tint",
    ),
    info_terms=("faq", "reviews", "warranty", "team", "estimate", "appointment", "schedule"),
    probe_paths=(
        "/services", "/our-services", "/auto-repair", "/auto-repair-services", "/service", "/repairs",
        "/about", "/about-us", "/contact", "/contact-us",
    ),
    fallback_paths=("/services", "/our-services", "/about", "/about-us", "/contact", "/contact-us"),
    signal_pattern=(
        r"\b(?:auto(?:motive)?\s+repairs?|car\s+repairs?|brake\s+(?:repair|service)|oil\s+changes?|collision\s+repair|"
        r"transmission\s+(?:repair|service)|wheel\s+alignment|diagnostics?|mechanics?|tire\s+(?:installation|rotation|service)|"
        r"auto\s+body|inventory|test\s+drive|pre-owned|new\s+(?:&|and)\s+used)\b"
    ),
    challenge_meta_terms=(
        "auto", "automotive", "repair", "collision", "tire", "brake", "car", "dealer", "service", "mechanic",
    ),
    fast_path_exclusions=False,
)

ENRICHMENT_HEADERS: list[str] = [
    "Qualification_Status",  # Qualified | Rejected | Needs Review | Unverified
    "Business_Type",
    "Services_Found",
    "Decision_Basis",  # Website | Website + Name | Name Only | Registry Default ...
    "Evidence_URLs",
    "Evidence_Summary",  # the reason for the verdict
    "Crawl_Status",
    "Classification_Confidence",
]

DECISION_COLUMN_PATTERNS = ("is auto repair", "auto repair ?", "is_auto_repair")

REPORT_HEADERS: list[str] = [
    "Input_Row",  # spreadsheet row number in the input file (header = row 1)
    "Business_Name",
    "dba_name",
    "Email",
    "Website",
    "Is Auto Repair ?",
    "Confidence",
    "Borderline",  # Yes = low confidence or conflicting signals: review this row
    "Business_Type",
    "Decision_Basis",
    "Reason",  # one line
    "Duplicate_Group",
    "Details",  # full reasoning
]


def auto_repair_crawler_config(read_timeout: float = 20.0) -> CrawlerConfig:
    """Patient, thorough crawl profile for auto repair qualification (mirrors the Christmas profile)."""
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
        max_deep_pages=4,
        max_probe_pages=6,
        respect_robots_txt=True,
        use_sitemap=True,
        max_sitemap_pages=4,
        impersonate_search_bot_on_challenge=False,
        render_javascript=True,
        block_separators=True,
        cache_namespace="crawler_auto_repair_v1",
        link_profile=AUTO_REPAIR_LINK_PROFILE,
    )


class AutoRepairLeadEnricher(NicheLeadEnricher[AutoRepairClassificationResult]):
    """Reads, crawls, classifies (per row), checkpoints, and saves auto repair leads."""

    pipeline_title = "Auto Repair Lead Classification Engine"
    summary_title = "Auto Repair Lead Classification Summary"
    classifier_version = CLASSIFIER_VERSION
    enrichment_headers = ENRICHMENT_HEADERS
    shares_result_per_site = False
    # Same Business_Name + same Email = duplicate: processed once, listed in duplicates.csv
    groups_duplicates = True
    # qualified_leads.csv lists each duplicate group once (first occurrence)
    dedupe_qualified_sheet = True
    reason_report_filename = "classification_report.csv"

    def __init__(
        self,
        classifier: AutoRepairClassifier | None = None,
        crawler: WebsiteCrawler | None = None,
        concurrency_limit: int = 8,
        timeout_seconds: float = 20.0,
        crawler_config: CrawlerConfig | None = None,
        max_subpages: int = 10,
        archive_fallback: bool = True,
    ) -> None:
        self.classifier = classifier or AutoRepairClassifier()
        self.crawler_config = crawler_config or auto_repair_crawler_config(timeout_seconds)
        super().__init__(
            crawler=crawler
            or WebsiteCrawler(timeout_seconds=timeout_seconds, max_subpages=max_subpages, config=self.crawler_config),
            concurrency_limit=concurrency_limit,
            archive_fallback=archive_fallback,
        )

    # ------------------------------------------------------------------ input handling
    def _column_map(self, headers: list[str]) -> ColumnMap:
        website = find_column(headers, "website", "clean website", "website url", "url", exact=True)
        if website is None:
            website = find_column(headers, "clean website", "website", "domain", "url")
        company = find_column(headers, "business_name", "business name", "company_name", "company name", "legal name")
        dba = find_column(headers, "dba_name", "dba name", "dba", "trade name", exact=True)
        if dba is None:
            dba = find_column(headers, "dba")
        existing = find_column(headers, *DECISION_COLUMN_PATTERNS)
        email = find_column(headers, "email", "e-mail", "email address", exact=True)
        if email is None:
            email = next(
                (i for i, h in enumerate(headers) if "email" in h.lower() and not h.lower().startswith(("has_", "has ", "is_"))),
                None,
            )
        return ColumnMap(
            website=website if website is not None else 0,
            company=company if company is not None else (dba if dba is not None else 0),
            existing_class=existing,
            alt_name=dba,
            extra={
                "dba": dba,
                "email": email,
                "website": website,
                "registry": find_column(headers, "license_type", "license type", exact=True),
            },
        )

    def _lead_fields(self, row: list[str], columns: ColumnMap) -> dict[str, str]:
        def cell(idx: int | None) -> str:
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        fields = {key: cell(idx) for key, idx in columns.extra.items()}
        fields["business"] = cell(columns.company)
        return fields

    @staticmethod
    def _lead_info(lead: EnrichmentLead) -> LeadInfo:
        f = lead.fields
        return LeadInfo(
            business_name=f.get("business", "") or lead.company,
            dba_name=f.get("dba", ""),
            registry=f.get("registry", ""),
        )

    @staticmethod
    def _norm_key(value: str) -> str:
        # "Big 10 Tire Stores, LLC" == "BIG 10 TIRE STORES LLC"
        return " ".join(re.sub(r"[\W_]+", " ", (value or "").casefold()).split())

    def _duplicate_key(self, lead: EnrichmentLead) -> str | None:
        """Same Business_Name AND same Email (case, spacing and punctuation ignored); both required."""
        business = self._norm_key(lead.fields.get("business", ""))
        email = (lead.fields.get("email", "") or "").strip().casefold()
        if not business or not email:
            return None
        return f"{business}\x1f{email}"

    def _duplicate_representative(self, group: list[EnrichmentLead]) -> EnrichmentLead:
        """Classify the group from the row whose name says the most about the trade (dba signal first, then
        signal strength, then any dba at all); ties keep the earliest row."""

        def informativeness(lead: EnrichmentLead) -> tuple[bool, float, bool]:
            dba = clean_dba(lead.fields.get("dba", ""))
            signal = analyze_name(lead.fields.get("business", ""), dba)
            return signal.source == "dba name", signal.strength, bool(dba)

        return max(group, key=informativeness)  # max() returns the first of equal rows

    # ------------------------------------------------------------------ crawling + classification
    async def _evaluate_group(
        self, crawler: WebsiteCrawler, leads: list[EnrichmentLead]
    ) -> dict[int, AutoRepairClassificationResult]:
        url = leads[0].crawl_url
        archived_snapshot = ""
        live_status = ""
        try:
            crawl: CrawlResult = await crawler.crawl_domain(url)
        except Exception as exc:
            logger.debug(f"Error crawling {url}: {exc}")
            parsed_domain = re.sub(r"^https?://(?:www\.)?", "", url).split("/")[0]
            crawl = CrawlResult(
                domain=parsed_domain,
                base_url=url,
                is_accessible=False,
                error_message=f"Unexpected error visiting {url}: {exc}",
                status_category="Connection Error",
            )

        crawl = downgrade_unusable(crawl)

        # Email subdomains ("acg.aaa.com") often have no website: try the registrable parent domain
        parent = self._parent_domain(crawl.domain)
        if not crawl.is_accessible and crawl.status_category == "DNS Failure" and parent:
            try:
                parent_crawl = downgrade_unusable(await crawler.crawl_domain(f"https://{parent}"))
            except Exception as exc:
                logger.debug(f"Error crawling parent domain {parent}: {exc}")
                parent_crawl = None
            if parent_crawl is not None and parent_crawl.is_accessible:
                parent_crawl.notes.append(f"{crawl.domain} does not resolve; evaluated parent domain {parent}")
                crawl = parent_crawl

        if (
            not crawl.is_accessible
            and self.archive_fallback
            and type(crawler) is WebsiteCrawler
            and crawl.status_category in ARCHIVE_FALLBACK_CATEGORIES
        ):
            try:
                archived = await crawler.crawl_archived(url, max_pages=3)
            except Exception as exc:
                logger.debug(f"Archive lookup failed for {url}: {exc}")
                archived = None
            if archived is not None:
                live_status = crawl.status_category
                archived_snapshot = archived.archived_snapshot
                archived.error_message = crawl.error_message
                crawl = downgrade_unusable(archived)

        profile = profile_site(crawl)
        results: dict[int, AutoRepairClassificationResult] = {}
        for lead in leads:
            results[lead.index] = self.classifier.classify(
                crawl,
                self._lead_info(lead),
                profile=profile,
                archived_snapshot=archived_snapshot,
                live_status=live_status,
            )
        return results

    @staticmethod
    def _parent_domain(domain: str) -> str:
        labels = (domain or "").lower().split(".")
        if len(labels) < 3 or len(labels[-2]) <= 3 and len(labels[-1]) == 2:  # skip "x.co.uk"-style suffixes
            return ""
        return ".".join(labels[-2:])

    def _result_from_dict(self, data: dict[str, Any]) -> AutoRepairClassificationResult:
        return AutoRepairClassificationResult.from_dict(data)

    def _missing_url_result(self, lead: EnrichmentLead) -> AutoRepairClassificationResult:
        return self.classifier.classify(None, self._lead_info(lead))

    def _needs_retry(self, res: AutoRepairClassificationResult) -> bool:
        if res.website_status.startswith("Archived Copy"):
            return any(f"live: {cat}" in res.website_status for cat in TRANSIENT_STATUS_CATEGORIES)
        if not res.site_accessible:
            return res.website_status in TRANSIENT_STATUS_CATEGORIES
        # Partial crawl that left the verdict to the name: worth a patient second look
        return "incomplete" in res.crawl_details and not res.decision_basis.startswith("Website")

    # ------------------------------------------------------------------ output
    def _enrichment_values(self, res: AutoRepairClassificationResult | None) -> list[str]:
        if res is None:
            return ["Pending"] + [""] * (len(ENRICHMENT_HEADERS) - 1)
        crawl_status = res.website_status
        if res.crawl_details:
            crawl_status = f"{crawl_status} | {res.crawl_details}"
        if not res.site_accessible and res.access_error:
            crawl_status = f"{crawl_status} | {res.access_error[:300]}"
        return [
            res.qualification_status,
            res.business_type,
            "; ".join(res.services_found),
            res.decision_basis,
            " | ".join(res.evidence_urls),
            res.reasoning,
            crawl_status,
            f"{res.confidence:.2f}",
        ]

    def _decision_value(self, res: AutoRepairClassificationResult) -> str:
        return res.decision_label

    def _status_label(self, res: AutoRepairClassificationResult) -> str:
        return f"{res.decision_label} ({res.confidence:.2f})"

    def _reason_report_headers(self) -> list[str]:
        return REPORT_HEADERS

    def _reason_report_row(
        self, lead: EnrichmentLead, res: AutoRepairClassificationResult | None, duplicate_group: str
    ) -> list[str]:
        f = lead.fields
        head = [str(lead.index + 2), f.get("business", ""), f.get("dba", ""), f.get("email", ""), f.get("website", "")]
        if res is None:
            return [*head, "", "", "", "", "", "Pending", duplicate_group, ""]
        return [
            *head,
            res.decision_label,
            f"{res.confidence:.2f}",
            "Yes" if res.borderline else "No",
            res.business_type,
            res.decision_basis,
            res.short_reason or res.reasoning[:200],
            duplicate_group,
            res.reasoning,
        ]

    # ------------------------------------------------------------------ QA
    def _site_evaluated(self, res: AutoRepairClassificationResult) -> bool:
        return res.site_accessible

    def _site_unreachable(self, res: AutoRepairClassificationResult) -> bool:
        return not res.site_accessible and res.website_status != "Missing URL"

    def _extra_validation(self, leads: list[EnrichmentLead], lead_result: Any) -> dict[str, Any]:
        decided = [r for lead in leads if (r := lead_result(lead)) is not None]
        return {
            "decision_column_filled": all(r.decision_label in ("Yes", "No") for r in decided),
            "low_confidence_for_review": sum(1 for r in decided if r.confidence < REVIEW_THRESHOLD),
            "borderline_rows": sum(1 for r in decided if r.borderline),
            "decided_from_name_only": sum(1 for r in decided if r.decision_basis.startswith(("Name", "Registry", "No Evidence"))),
        }
