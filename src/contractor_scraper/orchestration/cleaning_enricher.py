"""Cleaning business enrichment pipeline: is each business a residential cleaning company, which cleaning
services does its website advertise, and which one is its highest-value (main) service?

Same architecture, CLI style, concurrency, retries, caching, checkpoint/resume, second pass, archive
fallback, and output files as the auto repair pipeline (shared via `NicheLeadEnricher`). Per row it fills:

- the input's "Is Cleaning Company?" column with Yes / No (always decided; low-confidence verdicts are also
  routed to uncertain_leads.csv / unreachable_leads.csv for review),
- the input's "Main Service" column with ONE service: the highest-value residential service the website
  offers (left untouched when the website shows none),
- a "Services" column with every cleaning service the website advertises, named as the site writes them.

Website cells that are map-share / booking / chat links (share.google, g.co, wa.me, Jobber client hubs, ...)
are not business websites: the business-domain part of the row's email is crawled instead when there is one,
otherwise the row is decided from its name with low confidence. One domain is crawled once; each row is
classified separately against that crawl (its name and list category still count). Rows with the same
Business Name AND email are duplicates: classified once and listed in duplicates.csv.
"""

import re
from typing import Any
from urllib.parse import urlparse

from contractor_scraper.classification.auto_repair_classifier import downgrade_unusable
from contractor_scraper.classification.cleaning_service_classifier import (
    CLASSIFIER_VERSION,
    REVIEW_THRESHOLD,
    CleaningClassificationResult,
    CleaningServiceClassifier,
    LeadInfo,
    directory_link,
    non_site_link,
    profile_site,
)
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.crawler import (
    SOCIAL_PROFILE_DOMAINS,
    TRANSIENT_STATUS_CATEGORIES,
    CrawlerConfig,
    CrawlResult,
    LinkProfile,
    WebsiteCrawler,
)
from contractor_scraper.extraction.mail_domain import FREE_MAIL_PROVIDERS
from contractor_scraper.orchestration.niche_enricher import (
    ARCHIVE_FALLBACK_CATEGORIES,
    ColumnMap,
    EnrichmentLead,
    NicheLeadEnricher,
    find_column,
)

logger = get_logger("cleaning_enricher")

CLEANING_LINK_PROFILE = LinkProfile(
    name="cleaning",
    primary_terms=(
        "services", "service", "our-services", "what-we-do", "what we do", "residential", "house-cleaning", "house cleaning",
        "home-cleaning", "home cleaning", "maid", "deep-clean", "deep clean", "move-in", "move-out", "move in", "move out",
        "carpet", "upholstery", "rug", "tile", "grout", "window", "gutter", "roof", "pressure", "power-wash", "soft-wash",
        "soft wash", "house-wash", "house wash", "washing", "duct", "dryer", "vent", "chimney", "solar", "mold", "water-damage",
        "water damage", "fire", "restoration", "remediation", "biohazard", "hoard", "septic", "post-construction",
        "construction", "pool", "trash", "bin", "blinds", "airbnb", "vacation",
    ),
    secondary_terms=("cleaning", "clean", "pricing", "prices", "rates", "packages", "commercial", "janitorial", "office"),
    service_terms=("about", "about-us", "who-we-are", "who we are", "our-story", "faq", "areas", "service-area", "locations"),
    info_terms=("contact", "reviews", "testimonials", "gallery", "book", "quote", "estimate"),
    probe_paths=(
        "/services", "/our-services", "/residential-cleaning", "/residential", "/house-cleaning", "/what-we-do",
        "/about", "/about-us", "/pricing", "/contact",
    ),
    fallback_paths=("/services", "/our-services", "/residential-cleaning", "/about", "/about-us", "/contact"),
    signal_pattern=(
        r"\b(?:house|home|residential|maid|deep|move[\s-]?(?:in|out)|carpet|upholstery|tile|grout|window|gutter|roof|"
        r"pressure|power|soft|duct|dryer\s+vent|chimney|solar|mou?ld|water\s+damage|post[\s-]?construction|janitorial|"
        r"commercial|office)\s+(?:cleaning|washing|wash|remediation|restoration|services?)\b"
    ),
    challenge_meta_terms=("clean", "cleaning", "maid", "wash", "carpet", "janitorial", "window", "restoration", "service"),
    fast_path_exclusions=False,
)

ENRICHMENT_HEADERS: list[str] = [
    "Services",  # every cleaning service the website advertises, as written there ("; "-separated)
    "Qualification_Status",  # Qualified | Rejected | Needs Review | Unverified
    "Business_Type",
    "Decision_Basis",  # Website | Website + Name | Name Only | ...
    "Evidence_URLs",
    "Evidence_Summary",  # the reason for the verdict
    "Crawl_Status",
    "Classification_Confidence",
]
DECISION_COLUMN_PATTERNS = ("is cleaning company", "cleaning company?", "is_cleaning_company")
MAIN_SERVICE_PATTERNS = ("main service", "main_service", "primary service")
# Added only when the input has no such column
MAIN_SERVICE_HEADER = "Main Service"
DECISION_HEADER = "Is Cleaning Company?"

REPORT_HEADERS: list[str] = [
    "Input_Row",  # spreadsheet row number in the input file (header = row 1)
    "Business_Name",
    "Email",
    "Website",
    "Crawled_URL",
    "Is Cleaning Company?",
    "Main Service",
    "Services",
    "Confidence",
    "Borderline",  # Yes = low confidence or conflicting signals: review this row
    "Business_Type",
    "Decision_Basis",
    "Reason",  # one line
    "Duplicate_Group",
    "Details",  # full reasoning
]

# Regional ISPs and webmail hosts common in the list besides the shared free-mail set
EXTRA_FREE_MAIL: frozenset[str] = frozenset({
    "gci.net", "mtaonline.net", "alaska.net", "ptialaska.net", "acsalaska.net", "rocketmail.com", "aim.com", "mail.com", "gmx.com",
    "gmx.us", "frontier.com", "frontiernet.net", "windstream.net", "centurylink.net", "q.com", "embarqmail.com", "rr.com",
    "roadrunner.com", "twc.com", "optonline.net", "juno.com", "netzero.net", "netzero.com", "zoho.com", "yandex.com",
    "hotmail.co.uk", "yahoo.co.uk", "live.ca", "outlook.es", "pm.me", "tutanota.com", "fastmail.com", "inbox.com", "usa.net",
    "suddenlink.net", "mediacombb.net", "wowway.com", "cableone.net", "sparklight.net", "bresnan.net", "knology.net",
})


def is_free_mail_domain(domain: str) -> bool:
    d = (domain or "").strip().lower()
    if not d:
        return True
    return d in FREE_MAIL_PROVIDERS or d in EXTRA_FREE_MAIL or d.split(".")[0] in {"yahoo", "hotmail", "outlook", "live", "aol"}


def cleaning_crawler_config(read_timeout: float = 20.0) -> CrawlerConfig:
    """Patient, thorough crawl profile for cleaning qualification (mirrors the auto repair profile)."""
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
        cache_namespace="crawler_cleaning_v1",
        link_profile=CLEANING_LINK_PROFILE,
    )


class CleaningLeadEnricher(NicheLeadEnricher[CleaningClassificationResult]):
    """Reads, crawls, classifies (per row), checkpoints, and saves cleaning business leads."""

    pipeline_title = "Cleaning Business Enrichment & Classification Engine"
    summary_title = "Cleaning Business Classification Summary"
    classifier_version = CLASSIFIER_VERSION
    enrichment_headers = ENRICHMENT_HEADERS
    shares_result_per_site = False
    groups_duplicates = True
    dedupe_qualified_sheet = True
    reason_report_filename = "classification_report.csv"

    def __init__(
        self,
        classifier: CleaningServiceClassifier | None = None,
        crawler: WebsiteCrawler | None = None,
        concurrency_limit: int = 8,
        timeout_seconds: float = 20.0,
        crawler_config: CrawlerConfig | None = None,
        max_subpages: int = 10,
        archive_fallback: bool = True,
    ) -> None:
        self.classifier = classifier or CleaningServiceClassifier()
        self.crawler_config = crawler_config or cleaning_crawler_config(timeout_seconds)
        self.enrichment_headers = list(ENRICHMENT_HEADERS)
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
            website = find_column(headers, "website", "url")
        company = find_column(headers, "business name", "business_name", "company name", "company_name", "name", exact=True)
        if company is None:
            company = find_column(headers, "business name", "company", "name")
        existing = find_column(headers, *DECISION_COLUMN_PATTERNS)
        main_service = find_column(headers, *MAIN_SERVICE_PATTERNS)
        email = find_column(headers, "primary email", "email", "e-mail", "email address", exact=True)
        email_domain = find_column(headers, "domain", "email domain", exact=True)

        # Columns this mode fills that the input lacks are appended with the enrichment columns
        extra_headers = []
        if existing is None:
            extra_headers.append(DECISION_HEADER)
        if main_service is None:
            extra_headers.append(MAIN_SERVICE_HEADER)
        self.enrichment_headers = [*extra_headers, *ENRICHMENT_HEADERS]

        return ColumnMap(
            website=website if website is not None else 0,
            company=company if company is not None else 0,
            existing_class=existing,
            raw_website=email_domain,
            extra={
                "email": email,
                "website": website,
                "email_domain": email_domain,
                "email_type": find_column(headers, "email type", exact=True),
                "category": find_column(headers, "category (overture)", "category", exact=True),
                "segment": find_column(headers, "segment", exact=True),
                "main_service": main_service,
            },
        )

    def _lead_fields(self, row: list[str], columns: ColumnMap) -> dict[str, str]:
        def cell(idx: int | None) -> str:
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        fields = {key: cell(idx) for key, idx in columns.extra.items()}
        fields["business"] = cell(columns.company)
        if not fields.get("email_domain") and "@" in fields.get("email", ""):
            fields["email_domain"] = fields["email"].rsplit("@", 1)[-1].lower()
        return fields

    def _business_email_domain(self, fields: dict[str, str]) -> str:
        domain = (fields.get("email_domain") or "").strip().lower()
        if not domain and "@" in fields.get("email", ""):
            domain = fields["email"].rsplit("@", 1)[-1].strip().lower()
        if "free" in (fields.get("email_type") or "").lower() or is_free_mail_domain(domain):
            return ""
        if "." not in domain or non_site_link(domain) or directory_link(domain):
            return ""
        return domain

    def _row_crawl_url(self, row: list[str], columns: ColumnMap) -> str:
        """The website, unless it is a map-share / booking / directory link: then the email's business domain."""
        fields = self._lead_fields(row, columns)
        website = fields.get("website", "")
        url = self._normalize_url(website)
        if url and not non_site_link(url) and not directory_link(url):
            return url
        email_domain = self._business_email_domain(fields)
        if email_domain:
            return f"https://{email_domain}"
        # Social profiles: the crawler reads their public metadata; other non-site links are not crawlable
        host = urlparse(url).netloc.lower().removeprefix("www.") if url else ""
        if url and host in SOCIAL_PROFILE_DOMAINS:
            return url
        return ""

    def _website_note(self, lead: EnrichmentLead) -> str:
        website = lead.fields.get("website", "")
        host = non_site_link(website) or directory_link(website)
        if not host:
            return ""
        if lead.crawl_url and urlparse(lead.crawl_url).netloc.lower().removeprefix("www.") != host:
            return f"Website column holds a {host} link (not a business website); evaluated the email domain instead."
        return f"Website column holds a {host} link, not the business's own website."

    @staticmethod
    def _lead_info(lead: EnrichmentLead) -> LeadInfo:
        f = lead.fields
        return LeadInfo(
            business_name=f.get("business", "") or lead.company,
            category=f.get("category", ""),
            segment=f.get("segment", ""),
            website=f.get("website", ""),
        )

    @staticmethod
    def _norm_key(value: str) -> str:
        return " ".join(re.sub(r"[\W_]+", " ", (value or "").casefold()).split())

    def _duplicate_key(self, lead: EnrichmentLead) -> str | None:
        """Same Business Name AND same email (case, spacing and punctuation ignored); both required."""
        business = self._norm_key(lead.fields.get("business", ""))
        email = (lead.fields.get("email", "") or "").strip().casefold()
        if not business or not email:
            return None
        return f"{business}\x1f{email}"

    # ------------------------------------------------------------------ crawling + classification
    async def _evaluate_group(
        self, crawler: WebsiteCrawler, leads: list[EnrichmentLead]
    ) -> dict[int, CleaningClassificationResult]:
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
        results: dict[int, CleaningClassificationResult] = {}
        for lead in leads:
            note = self._website_note(lead)
            res = self.classifier.classify(
                crawl,
                self._lead_info(lead),
                profile=profile,
                archived_snapshot=archived_snapshot,
                live_status=live_status,
                website_note=note if not crawl.is_accessible else "",
            )
            if note and crawl.is_accessible:
                res.reasoning = f"{note} {res.reasoning}"
                res.crawl_details = "; ".join(filter(None, [res.crawl_details, note]))
            results[lead.index] = res
        return results

    def _result_from_dict(self, data: dict[str, Any]) -> CleaningClassificationResult:
        return CleaningClassificationResult.from_dict(data)

    def _missing_url_result(self, lead: EnrichmentLead) -> CleaningClassificationResult:
        return self.classifier.classify(None, self._lead_info(lead), website_note=self._website_note(lead))

    def _needs_retry(self, res: CleaningClassificationResult) -> bool:
        if res.website_status.startswith("Archived Copy"):
            return any(f"live: {cat}" in res.website_status for cat in TRANSIENT_STATUS_CATEGORIES)
        if not res.site_accessible:
            return res.website_status in TRANSIENT_STATUS_CATEGORIES
        # Partial crawl that left the verdict to the name: worth a patient second look
        return "incomplete" in res.crawl_details and not res.decision_basis.startswith("Website")

    # ------------------------------------------------------------------ output
    def _enrichment_values(self, res: CleaningClassificationResult | None) -> list[str]:
        n_extra = len(self.enrichment_headers) - len(ENRICHMENT_HEADERS)
        if res is None:
            return [""] * n_extra + [""] + ["Pending"] + [""] * (len(ENRICHMENT_HEADERS) - 2)
        crawl_status = res.website_status
        if res.crawl_details:
            crawl_status = f"{crawl_status} | {res.crawl_details}"
        if not res.site_accessible and res.access_error:
            crawl_status = f"{crawl_status} | {res.access_error[:300]}"
        extra: list[str] = []
        if DECISION_HEADER in self.enrichment_headers[:n_extra]:
            extra.append(res.decision_label)
        if MAIN_SERVICE_HEADER in self.enrichment_headers[:n_extra]:
            extra.append(res.main_service)
        return [
            *extra,
            "; ".join(res.services),
            res.qualification_status,
            res.business_type,
            res.decision_basis,
            " | ".join(res.evidence_urls),
            res.reasoning,
            crawl_status,
            f"{res.confidence:.2f}",
        ]

    def _decision_value(self, res: CleaningClassificationResult) -> str:
        return res.decision_label

    def _input_column_values(self, res: CleaningClassificationResult, columns: ColumnMap) -> dict[int, str]:
        col = columns.extra.get("main_service")
        if col is None:
            return {}
        return {col: res.main_service}  # empty = original cell kept

    def _status_label(self, res: CleaningClassificationResult) -> str:
        main = f" {res.main_service}" if res.main_service else ""
        return f"{res.decision_label}{main} ({res.confidence:.2f})"

    def _reason_report_headers(self) -> list[str]:
        return REPORT_HEADERS

    def _reason_report_row(
        self, lead: EnrichmentLead, res: CleaningClassificationResult | None, duplicate_group: str
    ) -> list[str]:
        f = lead.fields
        head = [str(lead.index + 2), f.get("business", ""), f.get("email", ""), f.get("website", ""), lead.crawl_url]
        if res is None:
            return [*head, "", "", "", "", "", "", "", "Pending", duplicate_group, ""]
        return [
            *head,
            res.decision_label,
            res.main_service,
            "; ".join(res.services),
            f"{res.confidence:.2f}",
            "Yes" if res.borderline else "No",
            res.business_type,
            res.decision_basis,
            res.short_reason or res.reasoning[:200],
            duplicate_group,
            res.reasoning,
        ]

    # ------------------------------------------------------------------ QA
    def _has_evidence(self, res: CleaningClassificationResult) -> bool:
        # A qualified cleaning company carries its reason and the main service taken from the website
        return bool(res.reasoning) and bool(res.main_service)

    def _site_evaluated(self, res: CleaningClassificationResult) -> bool:
        return res.site_accessible

    def _site_unreachable(self, res: CleaningClassificationResult) -> bool:
        return not res.site_accessible and res.website_status != "Missing URL"

    def _extra_validation(self, leads: list[EnrichmentLead], lead_result: Any) -> dict[str, Any]:
        decided = [r for lead in leads if (r := lead_result(lead)) is not None]
        return {
            "decision_column_filled": all(r.decision_label in ("Yes", "No") for r in decided),
            "qualified_with_main_service": sum(1 for r in decided if r.classification == "Yes" and r.main_service),
            "main_service_single_value": all(";" not in r.main_service for r in decided),
            "no_services_on_rejected": all(not r.services for r in decided if r.decision_label == "No"),
            "low_confidence_for_review": sum(1 for r in decided if r.confidence < REVIEW_THRESHOLD),
            "borderline_rows": sum(1 for r in decided if r.borderline),
            "decided_from_name_only": sum(1 for r in decided if not r.decision_basis.startswith("Website")),
        }
