"""High-precision classification engine for Christmas and Holiday Light Installation contractors.

Evaluates crawled website content (homepage, services pages, holiday/Christmas pages, about)
to determine whether a company genuinely provides on-site Christmas and holiday light installation
services for customers (residential/commercial installers, light hanging, setup, takedown & storage)
vs. product sellers/retailers/e-commerce stores, wholesale equipment distributors, landscape-only
lighting installers, general indoor electrical contractors, commercial security illumination, or
uncertain/unreachable sites.
"""

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any

from contractor_scraper.discovery.crawler import KNOWN_NON_CONTRACTOR_DOMAINS

if TYPE_CHECKING:
    from contractor_scraper.discovery.crawler import CrawlResult


# Specific multi-word service phrases indicating actual on-site Christmas/holiday/seasonal light installation
CHRISTMAS_SERVICE_OFFERING_PHRASES: tuple[str, ...] = (
    # Core Christmas and Holiday installation phrases
    "christmas light installation",
    "christmas lights installation",
    "christmas lighting installation",
    "holiday light installation",
    "holiday lights installation",
    "holiday lighting installation",
    "seasonal light installation",
    "seasonal lights installation",
    "seasonal lighting installation",
    "xmas light installation",
    "xmas lights installation",
    "xmas lighting installation",
    "seasonal lighting service",
    "seasonal lighting services",
    "holiday lighting service",
    "holiday lighting services",
    "christmas lighting service",
    "christmas lighting services",
    "christmas light service",
    "christmas lights service",
    "holiday light service",
    "holiday lights service",
    "holiday light hanging",
    "holiday lights hanging",
    "holiday lighting hanging",
    "christmas light hanging",
    "christmas lights hanging",
    "christmas lighting hanging",
    "hanging christmas lights",
    "hang christmas lights",
    "hang your christmas lights",
    "we hang christmas lights",
    "hanging holiday lights",
    "hang holiday lights",
    "hang your holiday lights",
    "we hang holiday lights",
    "we hang lights",
    "hangs christmas lights",
    "hangs holiday lights",
    "put up christmas lights",
    "putting up christmas lights",
    "put up holiday lights",
    "putting up holiday lights",
    "install christmas lights",
    "installing christmas lights",
    "install holiday lights",
    "installing holiday lights",
    "christmas light setup",
    "christmas lighting setup",
    "holiday light setup",
    "holiday lighting setup",
    "christmas light installers",
    "christmas light installer",
    "holiday light installers",
    "holiday light installer",
    "holiday lighting installers",
    "holiday lighting installer",
    "christmas lighting contractor",
    "christmas light contractor",
    "christmas lighting contractors",
    "christmas light contractors",
    "holiday lighting contractor",
    "holiday light contractor",
    "holiday lighting contractors",
    "holiday light contractors",
    "christmas light company",
    "christmas lighting company",
    "christmas lights company",
    "holiday light company",
    "holiday lighting company",
    "holiday lights company",
    "professional christmas light",
    "professional christmas lights",
    "professional christmas lighting",
    "professional holiday lighting",
    "professional holiday lights",
    "residential christmas light",
    "residential christmas lights",
    "residential christmas lighting",
    "residential holiday light",
    "residential holiday lights",
    "residential holiday lighting",
    "commercial christmas light",
    "commercial christmas lights",
    "commercial christmas lighting",
    "commercial holiday light",
    "commercial holiday lights",
    "commercial holiday lighting",
    # Specific holiday lighting service lifecycle (install, maintain, take down, store)
    "christmas light takedown",
    "holiday light takedown",
    "christmas light removal",
    "holiday light removal",
    "christmas light storage",
    "holiday light storage",
    "takedown and storage",
    "takedown & storage",
    "removal and storage",
    "removal & storage",
    "take down and store",
    "take down and storage",
    "take down & store",
    "take down & storage",
    "taking down and storing",
    "install, maintain, take down",
    "install, maintain, remove",
    "install, remove, and store",
    "installation, removal, and storage",
    "installation, takedown, and storage",
    "installation, maintenance, removal",
    "installation, maintenance, and storage",
    "custom holiday lighting design",
    "holiday lighting design and installation",
    "christmas lighting design and installation",
    "custom christmas light design",
    "custom holiday light design",
    "custom cut christmas lights",
    "custom cut holiday lights",
    "custom fit holiday lighting",
    "custom fit christmas lights",
    "commercial grade christmas lights",
    "commercial-grade christmas lights",
    "commercial grade holiday lights",
    "commercial-grade holiday lights",
    "christmas decor installation",
    "christmas decorating services",
    "christmas decorating service",
    "holiday decor installation",
    "holiday decorating services",
    "holiday decorating service",
    "holiday decor and lighting",
    "christmas decor and lighting",
    "holiday decor & lighting",
    "christmas decor & lighting",
    "outdoor holiday lighting installation",
    "exterior holiday lighting installation",
    "exterior christmas lighting installation",
    "roofline lighting installation",
    "roofline holiday lights",
    "roofline christmas lights",
    "roofline holiday lighting",
    "roofline christmas lighting",
    "c9 bulb installation",
    "c9 roofline",
    "c9 led bulbs",
    "c9 holiday lights",
    "c9 christmas lights",
    "perimeter roofline lighting",
    "tree wrapping with lights",
    "tree wrapping holiday lights",
    "trunk and branch wrapping",
    "bush and shrub lighting",
    "holiday greenery installation",
    "landscape and holiday lighting",
    "holiday and landscape lighting",
    "holiday & landscape lighting",
    "landscape & holiday lighting",
    "christmas light displays",
    "holiday light displays",
    "holiday illumination",
    "christmas illumination",
    "holiday lighting packages",
    "christmas lighting packages",
    "holiday light packages",
    "christmas light packages",
    "turnkey holiday lighting",
    "turnkey christmas lighting",
    "full service christmas lights",
    "full service holiday lighting",
    "all-inclusive holiday lighting",
    "all inclusive christmas lighting",
    "all-inclusive christmas light",
    "lease your christmas lights",
    "christmas light leasing",
    "holiday light leasing",
    # Permanent holiday lighting contractor systems
    "permanent christmas lights",
    "permanent christmas lighting",
    "permanent holiday lighting",
    "permanent holiday lights",
    "permanent roofline lighting",
    "permanent exterior lighting",
    "permanent decorative lighting",
    "rgb roofline lighting",
    "color-changing roofline lighting",
    "trimlight installation",
    "authorized trimlight dealer",
    "authorized trimlight installer",
    "trimlight dealer",
    "trimlight installer",
    "trimlight permanent lighting",
    "trimlight select",
    "jellyfish lighting installation",
    "authorized jellyfish lighting",
    "jellyfish permanent lighting",
    "everlights installation",
    "authorized everlights dealer",
    "everlights permanent lighting",
    "gemstone lights installation",
    "authorized gemstone lights",
    "gemstone permanent lighting",
    "invisa-lite installation",
    "celebright installation",
    "oelo lighting installation",
    "watts lights installation",
    "haven lighting installation",
    # Industry association signals
    "clipa certified",
    "clipa installer",
    "christmas light installation pros association",
    "christmas decor franchise",
    "we hang christmas lights franchise",
)

# Flexible multi-word regex patterns capturing real-world conversational & marketing phrasing
FLEXIBLE_CHRISTMAS_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Action verb -> Holiday/Christmas -> Lights/Lighting/Illumination
    re.compile(
        r"\b(install|installs|installing|installation|hang|hangs|hanging|put\s+up|putting\s+up|take\s+down|takedown|setup|rigging|removal)\b.{1,40}\b(christmas|holiday|xmas|seasonal)\b.{1,30}\b(lights?|lighting|illumination|displays?)\b",
        re.IGNORECASE,
    ),
    # Holiday/Christmas -> Lights/Lighting -> Action/Service/Contractor
    re.compile(
        r"\b(christmas|holiday|xmas|seasonal)\b.{1,30}\b(lights?|lighting|illumination|displays?)\b.{1,40}\b(installation|installer|installers|installing|hang|hanging|hanger|hangers|setup|takedown|take\s+down|removal|storage|service|services|package|packages|contractor|contractors|company|crew|pros|professionals)\b",
        re.IGNORECASE,
    ),
    # Complete contractor lifecycle service
    re.compile(
        r"\b(hang|install|lease)\b.{1,35}\b(maintain|maintenance)\b.{1,35}\b(take\s+down|takedown|remove|removal)\b.{1,35}\b(store|storage)\b",
        re.IGNORECASE,
    ),
    # Permanent holiday lighting systems
    re.compile(
        r"\b(trimlight|gemstone\s+lights?|jellyfish\s+lighting|everlights|invisa-lite|celebright|oelo\s+lighting|watts\s+lights?)\b",
        re.IGNORECASE,
    ),
    # Permanent + holiday/christmas + lights
    re.compile(
        r"\b(permanent)\b.{1,30}\b(holiday|christmas)\b.{1,30}\b(lights?|lighting)\b",
        re.IGNORECASE,
    ),
)

# Casual holiday mentions that DO NOT indicate light installation services
CASUAL_HOLIDAY_GREETING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(holiday\s+hours|holiday\s+schedule|happy\s+holidays|merry\s+christmas|closed\s+for\s+(the\s+)?holidays|closed\s+for\s+christmas|holiday\s+closure|holiday\s+break|christmas\s+party|holiday\s+party)\b",
        re.IGNORECASE,
    ),
)

# Operational contractor signals indicating active service delivery to homes/businesses
OPERATIONAL_CONTRACTOR_SIGNALS: tuple[str, ...] = (
    "free estimate",
    "get a free estimate",
    "request a quote",
    "request an estimate",
    "request your estimate",
    "request a consultation",
    "get a quote",
    "free quote",
    "schedule a consultation",
    "schedule an estimate",
    "schedule your consultation",
    "book an estimate",
    "book a consultation",
    "schedule installation",
    "schedule service",
    "schedule a service",
    "service area",
    "service areas",
    "areas we serve",
    "proudly serving",
    "counties served",
    "locally owned and operated",
    "family owned and operated",
    "licensed and insured",
    "licensed & insured",
    "fully insured",
    "our installers",
    "our installation crew",
    "our installation team",
    "professional installation team",
    "residential and commercial",
    "commercial and residential",
    "satisfaction guarantee",
    "we do all the work",
    "hassle-free holiday",
    "hassle free holiday",
    "we take down the lights",
    "we store the lights",
    "our design consultant",
    "consultation and design",
    "call today for a quote",
    "call for a free estimate",
)

# Multi-trade contractor indicators (landscapers, cleaners, electricians who add holiday lighting)
MULTI_TRADE_KEYWORDS: tuple[str, ...] = (
    "landscaping",
    "landscape design",
    "lawn care",
    "lawn service",
    "lawn maintenance",
    "window cleaning",
    "window washing",
    "pressure washing",
    "power washing",
    "gutter cleaning",
    "soft washing",
    "roof cleaning",
    "tree service",
    "electrical contractor",
    "licensed electrician",
    "electrical services",
    "painting contractor",
    "snow removal",
    "pest control",
    "handyman",
    "holiday decor",
)

# Permanent landscape lighting only (NO Christmas/holiday lighting service)
EXCLUSION_LANDSCAPE_ONLY_PHRASES: tuple[str, ...] = (
    "pathway lighting",
    "path lights",
    "garden lighting",
    "tree uplighting",
    "uplighting and downlighting",
    "low voltage lighting",
    "low voltage landscape",
    "architectural landscape lighting",
    "hardscape lighting",
    "deck and patio lighting",
    "patio lighting",
    "outdoor audio",
    "step lights",
    "bollard lighting",
    "well lights",
    "accent lighting",
    "submersible lighting",
    "fountain lighting",
    "landscape illumination",
)

# Indoor residential electrical phrases (bulbs, ceiling lights, repairs)
EXCLUSION_INDOOR_ELECTRICAL_PHRASES: tuple[str, ...] = (
    "ceiling fans",
    "recessed lighting",
    "indoor lighting fixtures",
    "circuit breakers",
    "panel upgrades",
    "electrical repairs",
    "rewiring",
    "switches and outlets",
    "electrical panel",
    "ev charger installation",
    "generators",
    "smoke detectors",
    "whole house surge",
    "chandeliers",
    "track lighting",
    "under cabinet lighting",
    "licensed electrician",
    "emergency electrical",
)

# Commercial security / high-mast illumination
EXCLUSION_COMMERCIAL_SECURITY_PHRASES: tuple[str, ...] = (
    "parking lot lighting",
    "security lighting",
    "pole lighting",
    "street lighting",
    "flood lights",
    "high bay lighting",
    "stadium lighting",
    "warehouse lighting",
    "commercial illumination",
)

# Event, DJ, party, wedding, stage, and entertainment lighting phrases (strictly excluded)
EXCLUSION_EVENT_ENTERTAINMENT_PHRASES: tuple[str, ...] = (
    "event lighting",
    "wedding lighting",
    "dj lighting",
    "party lighting",
    "stage lighting",
    "dance floor lighting",
    "disco lighting",
    "theatrical lighting",
    "concert lighting",
    "av rental",
    "audio visual rental",
    "photo booth",
    "dj services",
    "wedding dj",
    "special event illumination",
    "festival lighting",
)

# Disqualifying retail / e-commerce phrases (selling products rather than doing service)
DISQUALIFYING_RETAIL_PHRASES: tuple[str, ...] = (
    "add to cart",
    "shopping cart",
    "view cart",
    "proceed to checkout",
    "checkout now",
    "item(s) in cart",
    "items in cart",
    "buy online",
    "shop online",
    "shop now",
    "free shipping on orders",
    "order online",
    "product catalog",
    "browse our store",
    "cart subtotal",
    "spools of wire",
    "wholesale lighting",
    "wholesale prices",
    "wholesale distributor",
    "lighting distributor",
    "distributor of",
    "equipment supplier",
    "light bulbs for sale",
    "cases of c9",
    "replacement bulbs",
    "commercial lighting supplier",
    "commercial lighting source",
    "wholesale lighting company",
)

# Disqualifying unrelated big box / retail chains / unrelated trade phrases
DISQUALIFYING_UNRELATED_INDUSTRIES: tuple[str, ...] = (
    "grocery store",
    "supermarket",
    "apparel",
    "clothing store",
    "stationery store",
    "department store",
    "mattress store",
    "medical spa",
    "dental office",
    "real estate brokerage",
    "car dealership",
    "auto repair",
    "tire service",
    "tire shop",
    "discount tire",
    "nail salon",
    "veterinary clinic",
    "animal hospital",
    "public light show",
    "drive-through light display",
    "drive-thru light show",
    "botanical garden",
    "planetarium",
)

# Disqualifying software / SaaS phrases
DISQUALIFYING_TECH_PHRASES: tuple[str, ...] = (
    "software for contractors",
    "holiday lighting software",
    "field service management software",
    "routing and scheduling software",
    "book a demo",
    "request a demo",
    "pricing per user",
    "saas platform",
)

# Disqualifying directory / lead generator phrases
DISQUALIFYING_DIRECTORY_PHRASES: tuple[str, ...] = (
    "find christmas light installers near you",
    "find local christmas light",
    "find holiday lighting installers near you",
    "top 10 christmas light",
    "top 10 holiday lighting",
    "compare quotes from local",
    "directory of holiday lighting",
    "directory of christmas light",
    "directory of lighting contractors",
    "find pros near you",
)


@dataclass
class ChristmasLightClassificationResult:
    """Verdict indicating whether a business provides Christmas/Holiday light installation services."""

    classification: str  # "Yes" (Production), "No", "Uncertain", or "Unreachable"
    decision_label: str  # "Yes", "No", "Uncertain"
    confidence: float
    website_status: str  # "OK", "Timeout", "Connection Error", "Bot Challenge", "HTTP Error 404", etc.
    reasoning: str = ""
    access_error: str = ""
    evidence_phrases: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    operational_signals: list[str] = field(default_factory=list)

    @property
    def is_qualified(self) -> bool:
        """Returns True only if confirmed to provide Christmas light installation services."""
        return self.classification == "Yes"

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "decision_label": self.decision_label,
            "confidence": self.confidence,
            "website_status": self.website_status,
            "reasoning": self.reasoning,
            "access_error": self.access_error,
            "evidence_phrases": self.evidence_phrases,
            "disqualifiers": self.disqualifiers,
            "operational_signals": self.operational_signals,
        }


class ChristmasLightClassifier:
    """Examines crawled website pages to evaluate Christmas and holiday light installation service provision."""

    def evaluate_crawl(
        self,
        crawl: "CrawlResult",
        company_name: str | None = None,
    ) -> ChristmasLightClassificationResult:
        """Classify website content into Yes (Production), No (Not Qualified), Uncertain, or Unreachable."""
        # 1. Step 1: Inaccessible or Unreachable Website
        if not crawl.is_accessible:
            error_msg = crawl.error_message or "Failed to connect to website"
            status_cat = getattr(crawl, "status_category", "Connection Error") or "Connection Error"

            # Check if homepage title or meta description captured explicit service proof before block
            head_text = ""
            if crawl.homepage:
                head_text = f"{crawl.homepage.title} {crawl.homepage.meta_description}".lower()

            found_head_services = [
                p for p in CHRISTMAS_SERVICE_OFFERING_PHRASES if p in head_text
            ]
            if found_head_services:
                return ChristmasLightClassificationResult(
                    classification="Yes",
                    decision_label="Yes",
                    confidence=0.85,
                    website_status="Bot Challenge (Metadata Verified)",
                    evidence_phrases=found_head_services,
                    reasoning=(
                        f"Verified Christmas/holiday light installation contractor from website metadata: "
                        f"{', '.join(found_head_services)} (website protected by anti-bot challenge)."
                    ),
                )

            # Inaccessible site after recovery attempts
            return ChristmasLightClassificationResult(
                classification="Unreachable",
                decision_label="Uncertain",
                confidence=0.0,
                website_status=status_cat,
                reasoning=f"Website could not be accessed after recovery attempts: {error_msg}",
                access_error=error_msg,
            )

        # Fast check: known major national retail chains, department stores, supermarkets, venues
        clean_dom = (crawl.domain or "").lower().replace("www.", "")
        for ncd in KNOWN_NON_CONTRACTOR_DOMAINS:
            if ncd in clean_dom or clean_dom.endswith(ncd):
                return ChristmasLightClassificationResult(
                    classification="No",
                    decision_label="No",
                    confidence=0.99,
                    website_status="OK",
                    disqualifiers=["major national retailer/venue"],
                    reasoning=(
                        f"Identified as major national retail chain, department store, or public venue ({ncd}). "
                        "Sells retail goods and merchandise; does not provide contractor on-site Christmas or holiday light installation services."
                    ),
                )

        # 2. Extract and compile page text
        text_lower = (crawl.combined_text or "").lower()
        homepage_text = ""
        homepage_title = ""
        if crawl.homepage:
            homepage_title = (crawl.homepage.title or "").lower()
            homepage_text = f"{crawl.homepage.title} {crawl.homepage.meta_description} {crawl.homepage.text_content}".lower()

        # Compile subpages text, especially services and holiday pages
        services_text = ""
        holiday_subpage_present = False
        matched_service_urls: list[str] = []

        for sp in crawl.subpages:
            sp_url = sp.url.lower()
            sp_title = (sp.title or "").lower()
            is_holiday_sp = any(
                k in sp_url or k in sp_title
                for k in ["holiday", "christmas", "xmas", "seasonal", "light-hanging", "takedown", "trimlight", "gemstone", "jellyfish", "roofline"]
            )
            if is_holiday_sp:
                holiday_subpage_present = True
                matched_service_urls.append(sp.url)
            services_text += f" {sp.title} {sp.text_content}".lower()

        full_corpus = f"{homepage_title} {homepage_text} {services_text} {text_lower}"

        # 3. Identify signals across the corpus
        found_services: list[str] = [
            phrase for phrase in CHRISTMAS_SERVICE_OFFERING_PHRASES if phrase in full_corpus
        ]
        found_patterns: list[str] = []
        for pat in FLEXIBLE_CHRISTMAS_PATTERNS:
            m = pat.search(full_corpus)
            if m:
                found_patterns.append(m.group(0).strip()[:60])

        found_operations: list[str] = [
            phrase for phrase in OPERATIONAL_CONTRACTOR_SIGNALS if phrase in full_corpus
        ]
        found_multi_trade: list[str] = [
            kw for kw in MULTI_TRADE_KEYWORDS if kw in full_corpus
        ]
        found_landscape_only: list[str] = [
            phrase for phrase in EXCLUSION_LANDSCAPE_ONLY_PHRASES if phrase in full_corpus
        ]
        found_indoor_electrical: list[str] = [
            phrase for phrase in EXCLUSION_INDOOR_ELECTRICAL_PHRASES if phrase in full_corpus
        ]
        found_commercial_security: list[str] = [
            phrase for phrase in EXCLUSION_COMMERCIAL_SECURITY_PHRASES if phrase in full_corpus
        ]
        found_event_lighting: list[str] = [
            phrase for phrase in EXCLUSION_EVENT_ENTERTAINMENT_PHRASES if phrase in full_corpus
        ]
        found_retail: list[str] = [
            phrase for phrase in DISQUALIFYING_RETAIL_PHRASES if phrase in full_corpus
        ]
        found_unrelated: list[str] = [
            phrase for phrase in DISQUALIFYING_UNRELATED_INDUSTRIES if phrase in full_corpus
        ]
        found_tech: list[str] = [
            phrase for phrase in DISQUALIFYING_TECH_PHRASES if phrase in full_corpus
        ]
        found_dir: list[str] = [
            phrase for phrase in DISQUALIFYING_DIRECTORY_PHRASES if phrase in full_corpus
        ]

        all_disqualifiers = found_retail + found_unrelated + found_tech + found_dir

        # Token checks
        has_christmas_word = bool(re.search(r"\b(christmas|xmas)\b", full_corpus))
        has_holiday_word = bool(re.search(r"\b(holiday|holidays)\b", full_corpus))
        has_seasonal_word = bool(re.search(r"\b(seasonal)\b", full_corpus))
        has_lighting_word = bool(re.search(r"\b(lights?|lighting|illumination)\b", full_corpus))
        has_install_word = bool(
            re.search(r"\b(install|installation|hanging|hang|setup|takedown|removal|installer|installers)\b", full_corpus)
        )
        has_decor_word = bool(re.search(r"\b(decor|decorating|decorations|displays?)\b", full_corpus))
        has_casual_greeting = any(bool(pat.search(full_corpus)) for pat in CASUAL_HOLIDAY_GREETING_PATTERNS)

        word_count = len(full_corpus.split())

        # Collect verified holiday service evidence
        all_evidence: list[str] = list(found_services)
        for p_match in found_patterns:
            if p_match not in all_evidence:
                all_evidence.append(p_match)
        if holiday_subpage_present and (has_christmas_word or has_holiday_word) and has_install_word:
            all_evidence.append("dedicated holiday lighting service page")

        has_verified_service = len(all_evidence) > 0

        # 4. Step 2: Handle Disqualifiers (Unrelated retail stores, Tech, Directories)
        # Unrelated big box / grocery / apparel / department store (e.g. Muji, Lidl, Nordstrom)
        if found_unrelated and not has_verified_service:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.95,
                website_status="OK",
                disqualifiers=found_unrelated,
                reasoning=(
                    f"Identified as unrelated retail/commercial business ({', '.join(found_unrelated[:2])}). "
                    "Does not provide Christmas light installation services."
                ),
            )

        # Software / Tech platform
        if found_tech and not found_operations and not has_verified_service:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.92,
                website_status="OK",
                disqualifiers=found_tech,
                reasoning=f"Identified as software/technology platform ({', '.join(found_tech[:2])}).",
            )

        # Lead Directory / Portal
        if found_dir and not found_operations:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.90,
                website_status="OK",
                disqualifiers=found_dir,
                reasoning=f"Identified as contractor directory or lead portal ({', '.join(found_dir[:2])}).",
            )

        # Pure E-Commerce / Retail product seller (cart without service installation crew)
        if len(found_retail) >= 2 and not has_verified_service:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.92,
                website_status="OK",
                disqualifiers=found_retail,
                reasoning=(
                    f"Identified as lighting product seller / e-commerce store ({', '.join(found_retail[:2])}). "
                    "Sells products without offering installation services."
                ),
            )

        # Wholesale supplier
        if ("wholesale lighting" in full_corpus or "wholesale distributor" in full_corpus) and not has_verified_service:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.90,
                website_status="OK",
                disqualifiers=found_retail,
                reasoning="Identified as wholesale lighting distributor/supplier with no installation services.",
            )

        # 5. Step 3: Positive Service Qualification (YES / Production)
        # Qualifies dedicated Christmas/holiday lighting installers, permanent holiday lighting contractors,
        # and multi-trade contractors (landscaping, window cleaning, electricians, etc.) who offer holiday lighting.
        if has_verified_service:
            # Check if pure e-commerce store with shopping cart and zero contractor operations
            if len(found_retail) >= 3 and len(found_operations) == 0:
                return ChristmasLightClassificationResult(
                    classification="No",
                    decision_label="No",
                    confidence=0.88,
                    website_status="OK",
                    disqualifiers=found_retail,
                    reasoning=(
                        "Website sells holiday lighting products online (shopping cart/checkout present) "
                        "without evidence of active on-site installation service."
                    ),
                )

            confidence = min(0.99, 0.86 + (len(found_services) * 0.03) + (len(found_patterns) * 0.02) + (len(found_operations) * 0.02))
            reason_details = f"Found service offerings: {', '.join(all_evidence[:4])}."
            if matched_service_urls:
                reason_details += f" Confirmed on service page: {matched_service_urls[0]}."

            trade_context = ""
            if found_multi_trade:
                trade_context = f" Multi-trade contractor ({found_multi_trade[0]}) verified providing seasonal holiday lighting."

            return ChristmasLightClassificationResult(
                classification="Yes",
                decision_label="Yes",
                confidence=round(confidence, 3),
                website_status="OK",
                evidence_phrases=all_evidence[:8],
                operational_signals=found_operations[:5],
                disqualifiers=all_disqualifiers,
                reasoning=f"Confirmed Christmas/holiday light installation contractor. {reason_details}{trade_context}",
            )

        # 6. Step 4: Clear Negative Exclusions (NO)
        # Exclusion 1: Event, DJ, party, wedding, or entertainment lighting only
        if found_event_lighting:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.92,
                website_status="OK",
                reasoning=(
                    f"Provides event, DJ, party, wedding, or entertainment lighting ({', '.join(found_event_lighting[:2])}) "
                    "without genuine Christmas or holiday lighting installation services."
                ),
            )

        # Exclusion 2: Exclusively provides permanent landscape lighting without holiday lighting
        if found_landscape_only:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.92,
                website_status="OK",
                reasoning=(
                    f"Exclusively provides permanent landscape/architectural lighting "
                    f"({', '.join(found_landscape_only[:2])}) without Christmas or holiday lighting services."
                ),
            )

        # Exclusion 3: General electrical repairs / indoor residential lighting only
        if found_indoor_electrical:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.92,
                website_status="OK",
                reasoning=(
                    f"Provides general electrical/indoor lighting services ({', '.join(found_indoor_electrical[:2])}) "
                    "without Christmas or holiday lighting installation."
                ),
            )

        # Exclusion 4: Commercial / security / parking lot lighting only
        if found_commercial_security:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.90,
                website_status="OK",
                reasoning=(
                    f"Provides commercial/security illumination ({', '.join(found_commercial_security[:2])}) "
                    "without holiday or qualifying decorative lighting services."
                ),
            )

        # Exclusion 5: Casual holiday greeting (holiday hours, closed for Christmas) with NO lighting installation
        if has_casual_greeting and not (has_lighting_word and has_install_word):
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.88,
                website_status="OK",
                reasoning=(
                    "Mentions holiday/Christmas terms casually (e.g. holiday hours/greeting), "
                    "but does not offer any lighting installation services."
                ),
            )

        # Exclusion 6: Mentions holiday/Christmas terms without offering lighting services
        if (has_christmas_word or has_holiday_word) and not has_lighting_word and not has_install_word:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.88,
                website_status="OK",
                reasoning="Mentions holiday/Christmas terms without offering lighting services.",
            )

        # Exclusion 7: Active business website with NO holiday/seasonal lighting mentions
        if word_count >= 80:
            if not has_christmas_word and not has_holiday_word and not has_seasonal_word:
                return ChristmasLightClassificationResult(
                    classification="No",
                    decision_label="No",
                    confidence=0.90,
                    website_status="OK",
                    reasoning=(
                        "Active business website evaluated, but does not offer Christmas or holiday "
                        "lighting installation services."
                    ),
                )
            if has_lighting_word and not (has_christmas_word or has_holiday_word or has_seasonal_word):
                return ChristmasLightClassificationResult(
                    classification="No",
                    decision_label="No",
                    confidence=0.90,
                    website_status="OK",
                    reasoning=(
                        "Offers general light installation, but does not specifically provide Christmas or holiday lighting installation."
                    ),
                )

        # 7. Step 5: Insufficient Evidence / Uncertain Cases (UNCERTAIN)
        ambiguity_reasons: list[str] = []
        if word_count < 80:
            ambiguity_reasons.append(f"minimal text content ({word_count} words)")
        if (has_christmas_word or has_holiday_word) and not has_install_word:
            ambiguity_reasons.append("mentions holiday but no clear installation offering")
        if not ambiguity_reasons:
            ambiguity_reasons.append("insufficient service details to confirm on-site installation")

        return ChristmasLightClassificationResult(
            classification="Uncertain",
            decision_label="Uncertain",
            confidence=0.35,
            website_status="OK",
            reasoning=f"Insufficient evidence on website: {'; '.join(ambiguity_reasons)}.",
        )
