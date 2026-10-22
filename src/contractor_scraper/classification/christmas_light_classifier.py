"""High-precision classification engine for Christmas, Holiday, and Permanent Decorative Light Installation contractors.

Evaluates crawled website content page-by-page (homepage, services pages, holiday/Christmas pages,
permanent lighting pages, about) to determine whether a company genuinely provides on-site
installation of Christmas lights, holiday lights, or permanent decorative/roofline lighting systems
vs. product sellers/retailers/e-commerce stores, wholesale distributors, landscape-only lighting
installers, general electrical contractors, security lighting, event lighting, holiday light
attractions, or uncertain/unreachable sites.

Keywords are used to locate candidate evidence; every match is then interpreted in context
(sentence window, page purpose, negation, product-listing context, and business type) before it
counts toward qualification.
"""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from contractor_scraper.discovery.crawler import (
    KNOWN_NON_CONTRACTOR_DOMAINS,
    SOCIAL_PROFILE_DOMAINS,
)

if TYPE_CHECKING:
    from contractor_scraper.discovery.crawler import CrawlResult

# Bumped whenever the rules change so stale checkpointed verdicts are not reused
CLASSIFIER_VERSION = "2026.10-precision-1"

SERVICE_CHRISTMAS = "christmas"
SERVICE_HOLIDAY = "holiday"
SERVICE_PERMANENT = "permanent"
SERVICE_ROOFLINE = "roofline"

STRONG = "strong"
MODERATE = "moderate"
WEAK = "weak"


# Explicit multi-word phrases describing an actual installation service offering
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
    "christmas light takedown",
    "holiday light takedown",
    "christmas light removal",
    "holiday light removal",
    "christmas light storage",
    "holiday light storage",
    "custom holiday lighting design",
    "holiday lighting design and installation",
    "christmas lighting design and installation",
    "custom christmas light design",
    "custom holiday light design",
    "custom fit holiday lighting",
    "custom fit christmas lights",
    "holiday decor and lighting",
    "christmas decor and lighting",
    "holiday decor & lighting",
    "christmas decor & lighting",
    "outdoor holiday lighting installation",
    "exterior holiday lighting installation",
    "exterior christmas lighting installation",
    "roofline holiday lights",
    "roofline christmas lights",
    "roofline holiday lighting",
    "roofline christmas lighting",
    "c9 holiday lights",
    "c9 christmas lights",
    "landscape and holiday lighting",
    "holiday and landscape lighting",
    "holiday & landscape lighting",
    "landscape & holiday lighting",
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
    # Permanent holiday / decorative lighting systems
    "permanent christmas lights installation",
    "permanent holiday lighting installation",
    "permanent roofline lighting installation",
    "permanent holiday lighting installer",
    "permanent decorative lighting",
    "trimlight installation",
    "authorized trimlight dealer",
    "authorized trimlight installer",
    "trimlight dealer",
    "trimlight installer",
    "jellyfish lighting installation",
    "authorized jellyfish lighting",
    "jellyfish lighting dealer",
    "everlights installation",
    "authorized everlights dealer",
    "gemstone lights installation",
    "authorized gemstone lights",
    "gemstone lights dealer",
    "invisa-lite installation",
    "celebright installation",
    "oelo lighting installation",
    "oelo dealer",
    "watts lights installation",
    "haven lighting installation",
    "haven lighting dealer",
    # Industry association signals
    "clipa certified",
    "clipa installer",
    "christmas light installation pros association",
    "we hang christmas lights franchise",
)

# Lifecycle / installation phrases that only count when holiday or permanent-lighting context is nearby
CONTEXT_REQUIRED_PHRASES: tuple[str, ...] = (
    "takedown and storage",
    "takedown & storage",
    "take-down and storage",
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
    "set-up and take-down",
    "setup and takedown",
    "set up and take down",
    "we hang lights",
    "c9 bulb installation",
    "tree wrapping with lights",
    "tree wrapping holiday lights",
    "trunk and branch wrapping",
    "bush and shrub lighting",
)

_SENT = r"[^.!?|\n]"

# Decorating-service phrases: only a target (lighting) service when lights are named in the same passage
DECOR_SERVICE_PHRASES: tuple[str, ...] = (
    "christmas decor installation",
    "christmas decoration installation",
    "christmas decorating services",
    "christmas decorating service",
    "holiday decor installation",
    "holiday decoration installation",
    "holiday decorating services",
    "holiday decorating service",
    "holiday greenery installation",
    "commercial christmas decorating",
    "residential christmas decorating",
    "commercial holiday decorating",
    "residential holiday decorating",
)
DECOR_LIGHT_CONTEXT = re.compile(
    r"\b(lights|light\s+displays?|(?:christmas|xmas|holiday|seasonal|string|c9|c7|mini|led)\s+light(?:s|ing)?)\b",
    re.IGNORECASE,
)

# Flexible patterns capturing conversational/marketing phrasing of an installation service.
# Gaps never cross sentence or segment boundaries (_SENT), so separate menu items cannot combine.
FLEXIBLE_SERVICE_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    # (pattern, requires_holiday_context)
    # Install verb -> Holiday/Christmas -> Lights/Lighting
    (
        re.compile(
            rf"\b(install|installs|installing|installation|installed|put\s+up|putting\s+up|design\s+and\s+install)\b{_SENT}{{0,50}}?\b(christmas|xmas|holiday|seasonal)\b{_SENT}{{0,30}}?\b(lights?|lighting|light\s+displays?)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    # Hang / take-down verb -> Holiday/Christmas -> Lights/Lighting (not ornaments or decor items)
    (
        re.compile(
            rf"\b(hang|hangs|hanging|take\s+down|takedown)\b{_SENT}{{0,40}}?\b(christmas|xmas|holiday|seasonal)\s+(?:\w+\s+)?(lights?|lighting|light\s+displays?)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    # Holiday/Christmas -> Lights/Lighting -> Installation/Contractor noun
    (
        re.compile(
            rf"\b(christmas|xmas|holiday|seasonal)\s+(lights?|lighting)\b{_SENT}{{0,40}}?\b(installation|installations|installer|installers|install|installs|installing|installed|hanging|hanger|hangers|takedown|take[\s-]down|removal|contractors?|crews?)\b",
            re.IGNORECASE,
        ),
        False,
    ),
    # Complete contractor lifecycle service (hang, maintain, take down, store)
    (
        re.compile(
            rf"\b(hang|install|lease)\b{_SENT}{{1,60}}?\b(maintain|maintenance)\b{_SENT}{{1,60}}?\b(take\s+down|takedown|remove|removal)\b{_SENT}{{1,60}}?\b(store|storage)\b",
            re.IGNORECASE,
        ),
        True,
    ),
    # Christmas/Holiday Decor -> Outdoor lighting/systems
    (
        re.compile(
            rf"\b(christmas|holiday)\s+decor\b{_SENT}{{0,60}}?\b(outdoor\s+lighting|lighting\s+systems?|lights?|illumination)\b",
            re.IGNORECASE,
        ),
        True,
    ),
)

# Permanent decorative lighting language (strong only with decorative context AND installation context)
PERMANENT_LIGHTING_PATTERN = re.compile(
    r"\bpermanent\s+(?:(?:holiday|christmas|xmas|led|outdoor|exterior|roofline|architectural|accent|track|eave|smart|rgb|home|house|trim|decorative)\s+){0,3}(?:lights?|lighting|light\s+systems?|lighting\s+systems?)\b",
    re.IGNORECASE,
)
PERMANENT_HOLIDAY_PATTERN = re.compile(
    rf"\bpermanent\b{_SENT}{{0,40}}?\b(holiday|christmas|xmas)\b{_SENT}{{0,30}}?\b(lights?|lighting)\b",
    re.IGNORECASE,
)
PERMANENT_BRAND_PATTERN = re.compile(
    r"\b(trimlight|gemstone\s+lights?|jellyfish\s+lighting|everlights|oelo(?:\s+lighting)?|haven\s+lighting|celebright|invisa-?lite)\b",
    re.IGNORECASE,
)
# Roofline lighting is only a target service when holiday/decorative context confirms it
# (soffit lights are ordinary recessed fixtures and are deliberately not matched)
ROOFLINE_PATTERN = re.compile(r"\b(roofline|eave|eaves)\s+(?:led\s+)?(lights?|lighting)\b", re.IGNORECASE)
ROOFLINE_TERM = re.compile(r"roofline|\beaves?\b", re.IGNORECASE)
# Words inside a "permanent ... lighting" match that make it decorative by nature
PERMANENT_DECORATIVE_TERM = re.compile(r"holiday|christmas|xmas|roofline|eave|trim|rgb|smart|track|decorative", re.IGNORECASE)

# Decorative/holiday context near a permanent or roofline lighting mention (the mention itself is masked out)
PERMANENT_DECOR_CONTEXT = re.compile(
    r"\b(holiday|holidays|christmas|xmas|seasonal\s+lights?|every\s+(?:season|holiday|occasion)|color[\s-]?changing|millions\s+of\s+colou?rs|rgb|app[\s-]controlled|(?:smartphone|phone)\s+app|game\s+day|halloween|4th\s+of\s+july|fourth\s+of\s+july|no\s+more\s+(?:ladders|climbing|hanging)|never\s+(?:climb|hang)|trimlight|gemstone|jellyfish|oelo|everlights|celebright|roofline|eaves)\b",
    re.IGNORECASE,
)
ROOFLINE_DECOR_CONTEXT = re.compile(
    r"\b(holiday|holidays|christmas|xmas|seasonal|festive|c9|c7|permanent|color[\s-]?changing|rgb|app[\s-]controlled|trimlight|gemstone|jellyfish|oelo|everlights|celebright)\b",
    re.IGNORECASE,
)
PERMANENT_INSTALL_CONTEXT = re.compile(
    r"\b(install|installs|installed|installing|installation|installer|installers|dealer|authorized|certified|we\s+offer|we\s+provide|we\s+design|"
    r"we\s+now\s+offer|now\s+offering|(?:our\s+)?new(?:est)?\s+(?:addition|service|offering))\b",
    re.IGNORECASE,
)

# Generic mentions: located, then upgraded only with corroborating context
GENERIC_CHRISTMAS_PATTERN = re.compile(r"\b(christmas|xmas)\s+(lights?|lighting|light\s+displays?)\b", re.IGNORECASE)
GENERIC_HOLIDAY_PATTERN = re.compile(
    r"\b(holiday|seasonal)\s+(lights?|lighting|illumination|light\s+displays?)\b", re.IGNORECASE
)
# Decorating without lights is not one of the target services; at most it flags a lead for review
GENERIC_DECOR_PATTERN = re.compile(
    r"\b(christmas|xmas|holiday|seasonal|festive)\s+(decor|décor|decorations?|decorating)\b", re.IGNORECASE
)

HOLIDAY_CONTEXT_PATTERN = re.compile(
    r"\b(christmas|xmas|holiday|holidays|seasonal\s+light|permanent\s+(?:holiday|christmas|led|roofline)\s+light)", re.IGNORECASE
)
LIGHT_WORD_PATTERN = re.compile(r"\b(lights?|lighting|light\s+displays?)\b", re.IGNORECASE)
# Quote / estimate call-to-action: on a dedicated holiday-lighting page it shows the page sells the service
PAGE_CTA_PATTERN = re.compile(
    r"\b(free\s+(?:estimate|quote|consultation|design)|request\s+(?:a|an|your)\s+(?:quote|estimate|consultation)|"
    r"get\s+(?:a|your)\s+(?:free\s+)?(?:quote|estimate)|schedule\s+(?:a|an|your)\s+(?:consultation|estimate|installation)|"
    r"book\s+(?:a|an|your)\s+(?:consultation|estimate|installation))\b",
    re.IGNORECASE,
)
# Sentence context showing a lights mention is not an offered installation service
NON_SERVICE_MENTION_PATTERN = re.compile(
    r"\b(contest|competition|winners?|giveaway|parade|festival|tour|toy\s+drive|food\s+drive|fundraiser|donat\w*|charity|"
    r"hazards?|overload\w*|extension\s+cords?|circuits?|outlets?|unplug\w*|breakers?|tips?|safety|safely|light\s+show|"
    r"drive[\s-]th(?:ru|rough)|tickets?|admission)\b",
    re.IGNORECASE,
)
# A customer chore the business makes unnecessary ("no more hanging Christmas lights") is not that service
CHORE_CONTEXT_PATTERN = re.compile(
    r"\b(no\s+more|never\s+again|tired\s+of|instead\s+of|say\s+goodbye\s+to|forget\s+about|without\s+(?:having\s+to|the\s+hassle\s+of)|don't\s+have\s+to|no\s+need\s+to|stop)\W+(?:\w+\W+){0,6}$",
    re.IGNORECASE,
)
STRICT_SERVICE_CONTEXT_PATTERN = re.compile(
    r"\b(we\s+(?:offer|provide|install|hang|design|specialize|handle)|our\s+(?:team|crews?|installers?)|install(?:s|ed|ing|ation|ations|er|ers)?|"
    r"hang(?:s|ing)?|takedown|take[\s-]down|removal|free\s+(?:estimate|quote|consultation)|request\s+a\s+quote|get\s+a\s+quote)\b",
    re.IGNORECASE,
)
INSTALL_CONTEXT_PATTERN = re.compile(
    r"\b(install|installs|installed|installing|installation|installer|installers|hang|hanging|hung|dealer|authorized|certified|we\s+offer|we\s+provide|we\s+design|our\s+team|professional(?:ly)?|consultation|free\s+(?:estimate|quote)|get\s+a\s+quote|request\s+a\s+quote|estimate)\b",
    re.IGNORECASE,
)
SERVICE_CONTEXT_PATTERN = re.compile(
    r"\b(we\s+offer|we\s+provide|we\s+install|we\s+design|we\s+hang|our\s+services|services?|professional(?:ly)?|installed|install(?:ation)?|hang(?:ing)?|free\s+(?:estimate|quote)|request\s+a\s+quote|get\s+a\s+quote|consultation|custom\s+design|takedown|take[\s-]down)\b",
    re.IGNORECASE,
)
INSTALL_VERB_PATTERN = re.compile(
    r"\b(install|installs|installed|installing|installation|installer|installers|hang|hangs|hanging|hung|put\s+up|set\s?up|takedown|take[\s-]down|dealer|contractor|crew)\b",
    re.IGNORECASE,
)
NEGATION_PATTERN = re.compile(
    r"\b(no\s+longer|do\s+not|don't|does\s+not|doesn't|not\s+offer|not\s+provide|we\s+don't|discontinued|not\s+currently|stopped\s+offering)\b",
    re.IGNORECASE,
)
# Advice / tips / DIY phrasing directly preceding a match ("Before hanging holiday lights, check...")
ADVICE_CONTEXT_PATTERN = re.compile(
    r"\b(tips?|safety|before|when|while|how\s+to|diy|yourself|avoid|ways\s+to|guide\s+to|ideas?\s+for)\W+(?:\w+\W+){0,2}$",
    re.IGNORECASE,
)
PRODUCT_CONTEXT_PATTERN = re.compile(
    r"(add\s+to\s+(?:cart|bag)|\$\s?\d|\bsku\b|buy\s+now|in\s+stock|out\s+of\s+stock|sold\s+out|shop\s+now|per\s+strand|per\s+box|\bqty\b|quantity|free\s+shipping|@type\s+product|productgroup|pricecurrency|\bornaments?\b)",
    re.IGNORECASE,
)

# A page whose URL or title is about holiday / permanent decorative lighting itself
# (not merely "christmas" — e.g. /christmas-trees or /holiday-hours are not lighting pages)
DEDICATED_PAGE_PATTERN = re.compile(
    r"(christmas|xmas|holiday|seasonal)[\s_-]*(?:light|lighting|lights|decor|decorating|illumination)"
    r"|permanent[\s_-]*(?:holiday|christmas|xmas|led|roofline|track|lighting|lights)"
    r"|roofline[\s_-]*light|trimlight|gemstone[\s_-]*light|jellyfish[\s_-]*light|everlights|oelo|celebright"
    r"|light[\s_-]*hanging|hang[\s_-]*lights",
    re.IGNORECASE,
)

# Page types whose text describes products or gives advice rather than offering a service
EDITORIAL_PATH_PATTERN = re.compile(
    r"/(?:blogs?|posts?|news|articles?|resources|learn|guides?|insights|stories|press|category|categories|tag|tags|tips)(?:/|$)"
    r"|/\d{4}/\d{1,2}/"
    r"|[-_](?:tips|safety|how-to|ideas|checklist|mistakes|diy)(?:[-_/.]|$)",
    re.IGNORECASE,
)
EDITORIAL_TITLE_PATTERN = re.compile(r"\b(blog|news)\b", re.IGNORECASE)
ADVICE_TITLE_PATTERN = re.compile(
    r"\b(tips?|tricks|how[\s-]+to|safety|safely|guide|checklist|ideas|mistakes|diy|ways\s+to|steps\s+to)\b", re.IGNORECASE
)
PRODUCT_PATH_PATTERN = re.compile(r"/(?:products?|collections|shop|store|cart|catalog|catalogue|items?)(?:/|$)", re.IGNORECASE)
ARCHIVE_PATH_PREFIX = re.compile(r"^/web/\d+[a-z_]*/(?:https?:/+[^/]+)?", re.IGNORECASE)

# Business identity (name / domain / homepage title) centred on holiday or permanent decorative lighting
IDENTITY_PATTERN = re.compile(
    r"christmas|xmas|holiday[\s_-]*(?:light|decor)|permanent[\s_-]*light|trimlight|gemstone|jellyfish|everlights|oelo|celebright|twinkle|lights\b",
    re.IGNORECASE,
)

# Evidence contexts that never count toward qualification or review
NON_MEANINGFUL_CONTEXTS = frozenset({"advice", "product", "mention", "generic"})

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
    "free consultation",
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
    "locally owned",
    "family owned",
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
    "contact us for a quote",
    "get your quote",
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
    "roofing",
    "irrigation",
)

# Permanent landscape lighting only (NO Christmas/holiday lighting service)
EXCLUSION_LANDSCAPE_ONLY_PHRASES: tuple[str, ...] = (
    "landscape lighting",
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
    "floodlight installation",
    "high bay lighting",
    "stadium lighting",
    "warehouse lighting",
    "commercial illumination",
    "motion sensor lights",
)

# Event, DJ, party, wedding, stage, and entertainment lighting phrases
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

# Holiday light attractions / public displays (visitors, not customers of an installation service)
ATTRACTION_PHRASES: tuple[str, ...] = (
    "buy tickets",
    "purchase tickets",
    "tickets are",
    "admission",
    "drive-thru light",
    "drive-through light",
    "drive thru light",
    "light show",
    "festival of lights",
    "open nightly",
    "walk-through",
    "visit santa",
    "photos with santa",
    "hayrides",
    "museum",
    "shrine",
    "parish",
    "worship",
    "mass times",
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
    "lighting showroom",
    "visit our showroom",
    "@type product",
    "pricecurrency",
    "add to bag",
    "your cart is empty",
    "continue shopping",
    "sold out",
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

SERVICE_FIELD_YES = "Yes"
SERVICE_FIELD_NO = "No"
SERVICE_FIELD_UNCLEAR = "Unclear"


@dataclass
class EvidenceItem:
    """One contextual piece of evidence located on a specific page."""

    url: str
    term: str
    service: str  # christmas | holiday | permanent
    strength: str  # strong | moderate | weak
    snippet: str = ""
    # "" = describes a service. "editorial"/"decor" = would be evidence but is on a blog/article page or
    # names decorating without lights (review only). "advice"/"product"/"mention"/"generic" never count.
    context: str = ""
    base_strength: str = ""  # strength before the context downgrade

    @property
    def is_meaningful_weak(self) -> bool:
        """Weak evidence that still justifies manual review (never qualification)."""
        if self.strength != WEAK or self.context in NON_MEANINGFUL_CONTEXTS:
            return False
        return not self.context or self.base_strength in (STRONG, MODERATE)


@dataclass
class ChristmasLightClassificationResult:
    """Verdict indicating whether a business provides Christmas/Holiday/Permanent light installation services."""

    classification: str  # "Yes" (Qualified), "No", "Uncertain", or "Unreachable"
    decision_label: str  # "Yes", "No", "Uncertain"
    confidence: float
    website_status: str  # "OK", "Timeout", "Connection Error", "Bot Challenge", "HTTP Error 404", etc.
    reasoning: str = ""
    access_error: str = ""
    evidence_phrases: list[str] = field(default_factory=list)
    disqualifiers: list[str] = field(default_factory=list)
    operational_signals: list[str] = field(default_factory=list)
    christmas_service: str = SERVICE_FIELD_UNCLEAR
    holiday_service: str = SERVICE_FIELD_UNCLEAR
    permanent_service: str = SERVICE_FIELD_UNCLEAR
    roofline_service: str = SERVICE_FIELD_UNCLEAR
    evidence_urls: list[str] = field(default_factory=list)
    keywords_found: list[str] = field(default_factory=list)
    pages_evaluated: int = 0
    crawl_details: str = ""

    @property
    def is_qualified(self) -> bool:
        """Returns True only if confirmed to provide a target installation service."""
        return self.classification == "Yes"

    @property
    def qualification_status(self) -> str:
        return {
            "Yes": "Qualified",
            "No": "Rejected",
            "Uncertain": "Needs Review",
        }.get(self.classification, "Unverified")

    @property
    def qualified_niches(self) -> list[str]:
        """Target niches confirmed by evidence (only for Qualified leads)."""
        if self.classification != "Yes":
            return []
        fields = (
            (self.christmas_service, "Christmas Light Installation"),
            (self.holiday_service, "Holiday Light Installation"),
            (self.permanent_service, "Permanent Lighting Installation"),
            (self.roofline_service, "Roofline Lighting Installation"),
        )
        return [name for value, name in fields if value == SERVICE_FIELD_YES]

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
            "christmas_service": self.christmas_service,
            "holiday_service": self.holiday_service,
            "permanent_service": self.permanent_service,
            "roofline_service": self.roofline_service,
            "evidence_urls": self.evidence_urls,
            "keywords_found": self.keywords_found,
            "pages_evaluated": self.pages_evaluated,
            "crawl_details": self.crawl_details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChristmasLightClassificationResult":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def _service_for_term(term: str) -> str:
    t = term.lower()
    if "christmas" in t or "xmas" in t:
        return SERVICE_CHRISTMAS
    if re.search(r"permanent|trimlight|gemstone|jellyfish|everlights|oelo|haven|celebright|invisa", t):
        return SERVICE_PERMANENT
    if re.search(r"roofline|\beaves?\b", t):
        return SERVICE_ROOFLINE
    return SERVICE_HOLIDAY


def _window(text: str, start: int, end: int, radius: int) -> str:
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _segment_bounds(text: str, start: int, end: int, radius: int) -> tuple[int, int]:
    """Bounds of the block segment (between " | " separators) around a match, capped at `radius`."""
    left = text.rfind("|", max(0, start - radius), start)
    s = left + 1 if left != -1 else max(0, start - radius)
    right = text.find("|", end, end + radius)
    e = right if right != -1 else min(len(text), end + radius)
    return s, e


def _context(text: str, start: int, end: int, radius: int) -> str:
    """Text of the surrounding segment with the match itself masked out (so a term cannot confirm itself)."""
    s, e = _segment_bounds(text, start, end, radius)
    return f"{text[s:start]} … {text[end:e]}"


def _snippet(text: str, start: int, end: int, radius: int = 90) -> str:
    s = max(0, start - radius)
    e = min(len(text), end + radius)
    chunk = " ".join(text[s:e].split())
    return ("…" if s > 0 else "") + chunk + ("…" if e < len(text) else "")


def _domain_matches(domain: str, candidates: frozenset[str]) -> str | None:
    for c in candidates:
        if domain == c or domain.endswith("." + c) or c in domain:
            return c
    return None


class ChristmasLightClassifier:
    """Examines crawled website pages to evaluate Christmas, holiday, and permanent light installation services."""

    # ----------------------------------------------------------------- evidence extraction
    @staticmethod
    def _page_context(path: str, title: str, lower: str) -> str:
        """Page-level context: "product" listing, "advice" article, other "editorial" content, or "" (service page)."""
        path = ARCHIVE_PATH_PREFIX.sub("", path) or "/"
        if PRODUCT_PATH_PATTERN.search(path) or len(PRODUCT_CONTEXT_PATTERN.findall(lower)) >= 4:
            return "product"
        is_root = path.strip("/") == ""
        if not is_root and ADVICE_TITLE_PATTERN.search(title):
            return "advice"
        if EDITORIAL_PATH_PATTERN.search(path) or EDITORIAL_TITLE_PATTERN.search(title):
            return "advice" if ADVICE_TITLE_PATTERN.search(path.replace("-", " ").replace("_", " ")) else "editorial"
        return ""

    def _page_evidence(self, url: str, title: str, text: str, is_service_business: bool) -> list[EvidenceItem]:
        """Locate keyword matches on one page and grade each one by its surrounding context."""
        raw = f"{title}. {text}"
        lower = raw.lower()
        path = urlparse(url).path.lower()
        title_lower = title.lower()
        is_dedicated_page = bool(DEDICATED_PAGE_PATTERN.search(path) or DEDICATED_PAGE_PATTERN.search(title_lower))
        page_ctx = self._page_context(path, title_lower, lower)
        # Dedicated to lighting itself (a /holiday-decor page is dedicated, but not to lighting)
        is_dedicated_lighting_page = is_dedicated_page and bool(
            LIGHT_WORD_PATTERN.search(path.replace("-", " ").replace("_", " ")) or LIGHT_WORD_PATTERN.search(title_lower)
            or re.search(r"trimlight|gemstone|jellyfish|everlights|oelo|celebright", f"{path} {title_lower}")
        )
        sells_service_here = not page_ctx and is_service_business and bool(PAGE_CTA_PATTERN.search(lower))
        items: list[EvidenceItem] = []

        def negated(start: int) -> bool:
            s, _ = _segment_bounds(lower, start, start, 60)
            return bool(NEGATION_PATTERN.search(lower[s:start]))

        def product_context(start: int, end: int, matched: str) -> bool:
            win = _window(lower, start, end, 80)
            return bool(PRODUCT_CONTEXT_PATTERN.search(win)) and not INSTALL_VERB_PATTERN.search(matched)

        def non_service(start: int, end: int) -> bool:
            return bool(NON_SERVICE_MENTION_PATTERN.search(_context(lower, start, end, 120)))

        def mention_context(start: int, end: int, matched: str) -> str:
            if page_ctx == "product":
                if not INSTALL_VERB_PATTERN.search(matched):
                    return "product"
            elif page_ctx:
                return page_ctx
            if ADVICE_CONTEXT_PATTERN.search(lower[max(0, start - 40) : start]):
                return "advice"
            if INSTALL_VERB_PATTERN.search(matched) and CHORE_CONTEXT_PATTERN.search(lower[max(0, start - 60) : start]):
                return "mention"
            if product_context(start, end, matched):
                return "product"
            return ""

        def make(
            term: str, strength: str, start: int, end: int, service: str | None = None, ctx: str | None = None
        ) -> EvidenceItem:
            if ctx is None:
                ctx = mention_context(start, end, term)
            return EvidenceItem(
                url=url,
                term=term,
                service=service or _service_for_term(term),
                strength=WEAK if ctx else strength,
                snippet=_snippet(raw, start, end),
                context=ctx,
                base_strength=strength,
            )

        def add(term: str, strength: str, start: int, end: int, service: str | None = None, ctx: str | None = None) -> None:
            items.append(make(term, strength, start, end, service, ctx))

        # 1. Explicit installation-service phrases
        for phrase in CHRISTMAS_SERVICE_OFFERING_PHRASES:
            pos = lower.find(phrase)
            while pos != -1:
                end = pos + len(phrase)
                if not negated(pos):
                    add(phrase, STRONG, pos, end)
                    break
                pos = lower.find(phrase, end)

        # 1b. Decorating-service phrases: a lighting service only when lights are named in the same passage
        for phrase in DECOR_SERVICE_PHRASES:
            pos = lower.find(phrase)
            if pos == -1 or negated(pos):
                continue
            end = pos + len(phrase)
            if DECOR_LIGHT_CONTEXT.search(_context(lower, pos, end, 200)):
                add(phrase, STRONG, pos, end)
            else:
                add(phrase, MODERATE, pos, end, ctx=mention_context(pos, end, phrase) or "decor")

        # 2. Lifecycle phrases that need holiday / permanent-lighting context nearby
        for phrase in CONTEXT_REQUIRED_PHRASES:
            pos = lower.find(phrase)
            if pos == -1 or negated(pos):
                continue
            end = pos + len(phrase)
            ctx_text = _context(lower, pos, end, 250)
            if (HOLIDAY_CONTEXT_PATTERN.search(ctx_text) and DECOR_LIGHT_CONTEXT.search(ctx_text)) or is_dedicated_lighting_page:
                add(phrase, STRONG, pos, end, service=SERVICE_HOLIDAY)
            else:
                add(phrase, WEAK, pos, end, service=SERVICE_HOLIDAY, ctx="generic")

        # 3. Flexible sentence-level service patterns
        for pattern, needs_ctx in FLEXIBLE_SERVICE_PATTERNS:
            for m in pattern.finditer(lower):
                if negated(m.start()):
                    continue
                ctx_text = _context(lower, m.start(), m.end(), 200)
                if needs_ctx and not (
                    (HOLIDAY_CONTEXT_PATTERN.search(ctx_text) and DECOR_LIGHT_CONTEXT.search(f"{ctx_text} {m.group(0)}"))
                    or is_dedicated_lighting_page
                ):
                    continue
                matched = " ".join(m.group(0).split())
                ctx = "mention" if non_service(m.start(), m.end()) else None
                add(matched[:80], STRONG, m.start(), m.end(), ctx=ctx)
                break

        # 4. Permanent decorative and roofline lighting (decorative context + installation context required).
        #    The match itself is masked out of its context so e.g. "roofline lighting" cannot confirm itself.
        rank = {STRONG: 3, MODERATE: 2, WEAK: 1}
        for pattern in (PERMANENT_HOLIDAY_PATTERN, PERMANENT_LIGHTING_PATTERN, PERMANENT_BRAND_PATTERN, ROOFLINE_PATTERN):
            is_roofline = pattern is ROOFLINE_PATTERN
            service = SERVICE_ROOFLINE if is_roofline else SERVICE_PERMANENT
            best: EvidenceItem | None = None
            for m in pattern.finditer(lower):
                if negated(m.start()):
                    continue
                matched = " ".join(m.group(0).split())
                ctx_text = _context(lower, m.start(), m.end(), 220)
                if is_roofline:
                    decor_ctx = bool(ROOFLINE_DECOR_CONTEXT.search(ctx_text))
                else:
                    decor_ctx = (
                        pattern is PERMANENT_HOLIDAY_PATTERN
                        or pattern is PERMANENT_BRAND_PATTERN
                        or bool(PERMANENT_DECORATIVE_TERM.search(matched))
                        or bool(PERMANENT_DECOR_CONTEXT.search(ctx_text))
                    )
                install_ctx = bool(PERMANENT_INSTALL_CONTEXT.search(ctx_text)) or (is_dedicated_page and is_service_business)
                if decor_ctx and install_ctx:
                    candidate = make(matched, STRONG, m.start(), m.end(), service)
                elif decor_ctx and is_service_business and is_dedicated_page:
                    candidate = make(matched, MODERATE, m.start(), m.end(), service)
                elif decor_ctx:
                    candidate = make(matched, WEAK, m.start(), m.end(), service)
                else:
                    candidate = make(matched, WEAK, m.start(), m.end(), service, ctx="generic")
                if best is None or rank[candidate.strength] > rank[best.strength] or (
                    best.context == "generic" and candidate.context != "generic"
                ):
                    best = candidate
                if best.strength == STRONG:
                    break
            if best is not None:
                items.append(best)

        # 5. Generic Christmas / holiday lighting mentions, upgraded only with corroborating context
        for pattern in (GENERIC_CHRISTMAS_PATTERN, GENERIC_HOLIDAY_PATTERN):
            best = None
            for m in pattern.finditer(lower):
                if negated(m.start()):
                    continue
                matched = " ".join(m.group(0).split())
                if non_service(m.start(), m.end()):
                    candidate = make(matched, WEAK, m.start(), m.end(), ctx="mention")
                elif is_dedicated_lighting_page and sells_service_here:
                    # A service page about holiday lighting itself, with a quote/estimate call to action
                    candidate = make(matched, STRONG, m.start(), m.end())
                elif is_service_business and (
                    is_dedicated_page
                    or STRICT_SERVICE_CONTEXT_PATTERN.search(_context(lower, m.start(), m.end(), 100))
                ):
                    candidate = make(matched, MODERATE, m.start(), m.end())
                else:
                    candidate = make(matched, WEAK, m.start(), m.end())
                if best is None or rank[candidate.strength] > rank[best.strength] or (
                    best.context and not candidate.context
                ):
                    best = candidate
                if best.strength in (STRONG, MODERATE):
                    break
            if best is not None:
                items.append(best)

        # 6. Decorating mentions without lights: review-only, on a dedicated holiday page of a service business
        m = GENERIC_DECOR_PATTERN.search(lower)
        if m and not negated(m.start()):
            matched = " ".join(m.group(0).split())
            if is_service_business and is_dedicated_page:
                add(matched, MODERATE, m.start(), m.end(), ctx=mention_context(m.start(), m.end(), matched) or "decor")
            else:
                add(matched, WEAK, m.start(), m.end(), ctx="mention")

        return items

    # ----------------------------------------------------------------- main entry point
    def evaluate_crawl(
        self,
        crawl: "CrawlResult",
        company_name: str | None = None,
    ) -> ChristmasLightClassificationResult:
        """Classify website content into Yes (Qualified), No (Not Qualified), Uncertain, or Unreachable."""
        clean_dom = (crawl.domain or "").lower().replace("www.", "")
        pages_fetched = getattr(crawl, "pages_fetched", 0) or (len(crawl.subpages) + (1 if crawl.homepage else 0))
        crawl_details = self._crawl_details(crawl, pages_fetched)
        name_hint = ""
        if company_name and re.search(r"christmas|holiday|xmas|permanent|trimlight|lights?\b", company_name, re.IGNORECASE):
            name_hint = f" Business name ('{company_name}') suggests holiday lighting; manual verification recommended."

        # 1. Inaccessible or Unreachable Website
        if not crawl.is_accessible:
            error_msg = crawl.error_message or "Failed to connect to website"
            status_cat = getattr(crawl, "status_category", "Connection Error") or "Connection Error"

            # A blocked page can still expose metadata (title / description) describing the service. That is
            # not enough to verify the offering, so it flags the lead for review instead of qualifying it.
            if crawl.homepage and status_cat != "Parked Domain":
                head_raw = f"{crawl.homepage.title}. {crawl.homepage.meta_description}"
                head_items = [
                    i for i in self._page_evidence(crawl.homepage.url or crawl.base_url, "", head_raw, False) if i.strength == STRONG
                ]
                if head_items:
                    best = head_items[0]
                    return ChristmasLightClassificationResult(
                        classification="Uncertain",
                        decision_label="Uncertain",
                        confidence=0.45,
                        website_status=status_cat + " (Metadata Only)",
                        reasoning=(
                            f"Website blocked access ({error_msg}; not bypassed). Its published title/description claims "
                            f"\"{best.term}\" (\"{best.snippet[:160]}\"), but no service page could be inspected; "
                            "manual verification required."
                        ),
                        access_error=error_msg,
                        evidence_phrases=list(dict.fromkeys(i.term for i in head_items))[:8],
                        evidence_urls=[crawl.homepage.url or crawl.base_url],
                        keywords_found=self._keywords(head_items),
                        pages_evaluated=0,
                        crawl_details=crawl_details,
                    )

            return ChristmasLightClassificationResult(
                classification="Unreachable",
                decision_label="Uncertain",
                confidence=0.0,
                website_status=status_cat,
                reasoning=f"Website could not be accessed after recovery attempts: {error_msg}.{name_hint}",
                access_error=error_msg,
                crawl_details=crawl_details,
            )

        # 2. Known major national retail chains, department stores, supermarkets, venues
        ncd = _domain_matches(clean_dom, KNOWN_NON_CONTRACTOR_DOMAINS)
        if ncd:
            return ChristmasLightClassificationResult(
                classification="No",
                decision_label="No",
                confidence=0.99,
                website_status="OK",
                disqualifiers=["major national retailer/venue"],
                christmas_service=SERVICE_FIELD_NO,
                holiday_service=SERVICE_FIELD_NO,
                permanent_service=SERVICE_FIELD_NO,
                roofline_service=SERVICE_FIELD_NO,
                evidence_urls=[crawl.base_url] if crawl.base_url else [],
                reasoning=(
                    f"Identified as major national retail chain, department store, or public venue ({ncd}). "
                    "Sells retail goods and merchandise; does not provide contractor on-site Christmas or holiday light installation services."
                ),
                crawl_details=crawl_details,
            )

        # 3. Assemble per-page corpus
        page_entries: list[tuple[str, str, str]] = []  # (url, title, text)
        for p in crawl.all_pages if hasattr(crawl, "all_pages") else []:
            page_entries.append((p.url or crawl.base_url, p.title or "", f"{p.meta_description or ''}. {p.text_content or ''}"))
        combined = crawl.combined_text or ""
        homepage_text = crawl.homepage.text_content if crawl.homepage else ""
        if combined and not crawl.subpages and (not homepage_text or homepage_text not in combined):
            page_entries.append((crawl.base_url, "", combined))

        full_corpus = " ".join(f"{t} {x}" for _, t, x in page_entries).lower()
        word_count = sum(1 for w in " ".join(x for _, _, x in page_entries).split() if w != "|")

        found_operations = [p for p in OPERATIONAL_CONTRACTOR_SIGNALS if p in full_corpus]
        found_multi_trade = [k for k in MULTI_TRADE_KEYWORDS if k in full_corpus]
        found_landscape_only = [p for p in EXCLUSION_LANDSCAPE_ONLY_PHRASES if p in full_corpus]
        found_indoor_electrical = [p for p in EXCLUSION_INDOOR_ELECTRICAL_PHRASES if p in full_corpus]
        found_commercial_security = [p for p in EXCLUSION_COMMERCIAL_SECURITY_PHRASES if p in full_corpus]
        found_event_lighting = [p for p in EXCLUSION_EVENT_ENTERTAINMENT_PHRASES if p in full_corpus]
        found_attraction = [p for p in ATTRACTION_PHRASES if p in full_corpus]
        found_retail = [p for p in DISQUALIFYING_RETAIL_PHRASES if p in full_corpus]
        found_unrelated = [p for p in DISQUALIFYING_UNRELATED_INDUSTRIES if p in full_corpus]
        found_tech = [p for p in DISQUALIFYING_TECH_PHRASES if p in full_corpus]
        found_dir = [p for p in DISQUALIFYING_DIRECTORY_PHRASES if p in full_corpus]
        has_casual_greeting = any(pat.search(full_corpus) for pat in CASUAL_HOLIDAY_GREETING_PATTERNS)
        all_disqualifiers = found_retail + found_unrelated + found_tech + found_dir

        is_service_business = bool(found_operations or found_multi_trade)
        retail_dominant = len(found_retail) >= 2 and not found_operations

        # 4. Contextual evidence, page by page
        evidence: list[EvidenceItem] = []
        for url, title, text in page_entries:
            evidence.extend(self._page_evidence(url, title, text, is_service_business))

        positive = [e for e in evidence if e.strength in (STRONG, MODERATE)]
        strong = [e for e in evidence if e.strength == STRONG]
        moderate = [e for e in evidence if e.strength == MODERATE]
        weak = [e for e in evidence if e.strength == WEAK]
        homepage_title = crawl.homepage.title if crawl.homepage else ""
        identity_match = bool(IDENTITY_PATTERN.search(f"{company_name or ''} {clean_dom} {homepage_title}"))
        # Qualification needs an explicit service statement, or listing-level evidence corroborated by a
        # second page or by a business identity centred on holiday / permanent decorative lighting.
        qualifies = bool(strong) or len({e.url for e in moderate}) >= 2 or (bool(moderate) and identity_match)
        strong_has_install_verb = any(INSTALL_VERB_PATTERN.search(e.term) for e in strong)

        # 5. Disqualifiers (unrelated retail stores, tech, directories, product sellers, attractions)
        if found_unrelated and not positive and not found_operations:
            return self._not_qualified(
                f"Identified as unrelated retail/commercial business ({', '.join(found_unrelated[:2])}). "
                "Does not provide Christmas light installation services.",
                0.95, crawl, crawl_details, pages_fetched, evidence, found_unrelated,
            )
        if found_tech and not found_operations and not positive:
            return self._not_qualified(
                f"Identified as software/technology platform ({', '.join(found_tech[:2])}).",
                0.92, crawl, crawl_details, pages_fetched, evidence, found_tech,
            )
        if found_dir and not found_operations and not strong:
            return self._not_qualified(
                f"Identified as contractor directory or lead portal ({', '.join(found_dir[:2])}).",
                0.90, crawl, crawl_details, pages_fetched, evidence, found_dir,
            )
        if ("wholesale lighting" in full_corpus or "wholesale distributor" in full_corpus) and not positive:
            return self._not_qualified(
                "Identified as wholesale lighting distributor/supplier with no installation services.",
                0.90, crawl, crawl_details, pages_fetched, evidence, found_retail,
            )
        if retail_dominant and not (strong and strong_has_install_verb):
            return self._not_qualified(
                f"Identified as lighting/holiday product seller or e-commerce store ({', '.join(found_retail[:2])}). "
                "Sells products without evidence of an on-site installation service.",
                0.90, crawl, crawl_details, pages_fetched, evidence, found_retail,
            )
        if len(found_attraction) >= 2 and not strong:
            return self._not_qualified(
                f"Holiday light attraction, public display, or venue ({', '.join(found_attraction[:2])}); "
                "visitors attend displays, but no installation service is offered to customers.",
                0.88, crawl, crawl_details, pages_fetched, evidence, found_attraction,
            )

        # 6. Positive qualification
        if qualifies:
            dedicated_strong = any(
                e.strength == STRONG and DEDICATED_PAGE_PATTERN.search(urlparse(e.url).path.lower()) for e in positive
            )
            if strong:
                base = 0.93 if dedicated_strong else 0.88
            else:
                base = 0.74
            extra_pages = len({e.url for e in positive}) - 1
            confidence = min(0.98, base + 0.02 * extra_pages + 0.01 * min(3, len(found_operations)))
            trade_context = ""
            if found_multi_trade:
                trade_context = f" Multi-trade contractor ({found_multi_trade[0]}) verified providing seasonal/decorative lighting installation."
            return self._qualified(
                positive, weak, found_operations, confidence, "OK", crawl_details, pages_fetched,
                extra_reason=trade_context, disqualifiers=all_disqualifiers, all_evidence=evidence,
            )

        # 7. Listing-level evidence that is not corroborated: needs manual review, never qualified
        if moderate:
            terms = list(dict.fromkeys(e.term for e in moderate))[:4]
            return self._uncertain(
                f"Needs review: the site lists {', '.join(repr(t) for t in terms)} ({moderate[0].url}) but no page "
                "explicitly states a Christmas, holiday, permanent, or roofline lighting installation service.",
                0.50, crawl, crawl_details, pages_fetched, evidence, thorough=True,
            )

        # 8. Weak signals only: ambiguous, needs verification (product listings and DIY/safety tips are not service signals)
        meaningful_weak = [e for e in weak if e.is_meaningful_weak]
        if meaningful_weak:
            terms = list(dict.fromkeys(e.term for e in meaningful_weak))[:4]
            return self._uncertain(
                f"Insufficient evidence of an installation service: site mentions {', '.join(repr(t) for t in terms)} "
                "but no page clearly offers Christmas, holiday, or permanent decorative lighting installation.",
                0.40, crawl, crawl_details, pages_fetched, evidence, thorough=True,
            )

        # 9. No holiday / decorative lighting signals at all
        is_social = _domain_matches(clean_dom, SOCIAL_PROFILE_DOMAINS) is not None
        if is_social:
            return ChristmasLightClassificationResult(
                classification="Unreachable",
                decision_label="Uncertain",
                confidence=0.0,
                website_status="Social Media Profile",
                reasoning=f"Social media profile requires login; public metadata did not describe the services offered.{name_hint}",
                access_error="Social media profile content requires login (not bypassed)",
                crawl_details=crawl_details,
            )

        exclusion_reason = self._exclusion_reason(
            found_event_lighting, found_landscape_only, found_indoor_electrical, found_commercial_security
        )
        js_shell = any(getattr(p, "is_js_shell", False) for p in crawl.all_pages) if hasattr(crawl, "all_pages") else False
        relevant_unevaluated = self._relevant_unevaluated(crawl)

        if relevant_unevaluated:
            return self._uncertain(
                f"Insufficient evidence: a potentially relevant page could not be evaluated ({relevant_unevaluated[0]}).{name_hint}",
                0.35, crawl, crawl_details, pages_fetched, evidence, thorough=False,
            )
        if exclusion_reason and not getattr(crawl, "incomplete_reason", ""):
            return self._not_qualified(exclusion_reason, 0.90, crawl, crawl_details, pages_fetched, evidence, [])
        # A short site can still clearly describe a different trade; only content-less sites are uncertain
        describes_other_service = bool(found_multi_trade or found_operations) and not js_shell
        if word_count < 80 and not (describes_other_service and not getattr(crawl, "incomplete_reason", "")):
            reason = f"Insufficient evidence on website: minimal text content ({word_count} words)"
            if js_shell:
                reason += "; site is JavaScript-rendered and static HTML exposed little content"
            return self._uncertain(reason + "." + name_hint, 0.30, crawl, crawl_details, pages_fetched, evidence, thorough=False)
        if getattr(crawl, "incomplete_reason", "") and word_count < 400:
            return self._uncertain(
                f"Insufficient evidence: crawl incomplete ({crawl.incomplete_reason}).{name_hint}",
                0.30, crawl, crawl_details, pages_fetched, evidence, thorough=False,
            )
        if exclusion_reason:
            return self._not_qualified(exclusion_reason, 0.88, crawl, crawl_details, pages_fetched, evidence, [])

        offering = f" ({', '.join(found_multi_trade[:3])})" if found_multi_trade else ""
        if has_casual_greeting:
            reason = (
                "Mentions holiday/Christmas terms only casually (e.g. holiday hours/greeting); "
                f"the evaluated pages describe a different service offering{offering} with no lighting installation."
            )
        else:
            reason = (
                f"Active business website evaluated across its relevant pages; it describes a different service offering{offering} "
                "and does not offer Christmas, holiday, or permanent decorative lighting installation."
            )
        confidence = 0.90 if pages_fetched >= 3 else 0.85
        return self._not_qualified(reason, confidence, crawl, crawl_details, pages_fetched, evidence, [])

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _crawl_details(crawl: "CrawlResult", pages_fetched: int) -> str:
        parts: list[str] = []
        if crawl.is_accessible:
            parts.append(f"{pages_fetched} page(s) evaluated")
        if getattr(crawl, "total_attempts", 0):
            parts.append(f"{crawl.total_attempts} request attempt(s)")
        if getattr(crawl, "sitemap_checked", False):
            parts.append("sitemap checked")
        if getattr(crawl, "failed_urls", None):
            parts.append(f"{len(crawl.failed_urls)} page(s) failed")
        if getattr(crawl, "robots_blocked_urls", None):
            parts.append(f"{len(crawl.robots_blocked_urls)} page(s) disallowed by robots.txt")
        if getattr(crawl, "from_cache", False):
            parts.append("cached crawl")
        if getattr(crawl, "incomplete_reason", ""):
            parts.append(f"incomplete: {crawl.incomplete_reason}")
        parts.extend(getattr(crawl, "notes", []) or [])
        return "; ".join(parts)

    @staticmethod
    def _relevant_unevaluated(crawl: "CrawlResult") -> list[str]:
        failed = [f.split(" (", 1)[0].rstrip("/") for f in getattr(crawl, "failed_urls", []) or []]
        relevant = {u.rstrip("/") for u in getattr(crawl, "discovered_relevant_urls", []) or []}
        blocked = [u.rstrip("/") for u in getattr(crawl, "robots_blocked_urls", []) or []]
        return [u for u in failed + blocked if u in relevant]

    @staticmethod
    def _exclusion_reason(event: list[str], landscape: list[str], indoor: list[str], security: list[str]) -> str:
        if event:
            return (
                f"Provides event, DJ, party, wedding, or entertainment lighting ({', '.join(event[:2])}) "
                "without Christmas or holiday lighting installation services."
            )
        if len(landscape) >= 2 or (landscape and not indoor):
            return (
                f"Provides landscape/architectural lighting only ({', '.join(landscape[:3])}) "
                "without Christmas, holiday, or permanent decorative lighting installation."
            )
        if indoor:
            return (
                f"Provides general electrical/indoor lighting services ({', '.join(indoor[:3])}) "
                "without Christmas or holiday lighting installation."
            )
        if security:
            return (
                f"Provides commercial/security illumination ({', '.join(security[:2])}) "
                "without holiday or qualifying decorative lighting services."
            )
        return ""

    @staticmethod
    def _service_fields(
        evidence: list[EvidenceItem], thorough: bool
    ) -> tuple[str, str, str, str]:
        def field_for(service: str) -> str:
            items = [e for e in evidence if e.service == service]
            if service == SERVICE_ROOFLINE:
                # Holiday/permanent service evidence that names the roofline ("roofline christmas lights",
                # "permanent holiday lighting along your roofline")
                items += [
                    e for e in evidence
                    if e.service != service and e.strength in (STRONG, MODERATE)
                    and (ROOFLINE_TERM.search(e.term) or ROOFLINE_TERM.search(e.snippet))
                ]
            if any(e.strength in (STRONG, MODERATE) for e in items):
                return SERVICE_FIELD_YES
            if any(e.context not in NON_MEANINGFUL_CONTEXTS for e in items) or not thorough:
                return SERVICE_FIELD_UNCLEAR
            return SERVICE_FIELD_NO

        christmas = field_for(SERVICE_CHRISTMAS)
        holiday = field_for(SERVICE_HOLIDAY)
        permanent = field_for(SERVICE_PERMANENT)
        roofline = field_for(SERVICE_ROOFLINE)
        # Christmas light installation is a holiday light installation service
        if christmas == SERVICE_FIELD_YES:
            holiday = SERVICE_FIELD_YES
        return christmas, holiday, permanent, roofline

    @staticmethod
    def _keywords(evidence: list[EvidenceItem]) -> list[str]:
        return list(dict.fromkeys(e.term for e in evidence))[:15]

    @staticmethod
    def _inspected_urls(crawl: "CrawlResult") -> list[str]:
        urls = [p.url for p in crawl.all_pages] if hasattr(crawl, "all_pages") else []
        if not urls and crawl.base_url:
            urls = [crawl.base_url]
        return list(dict.fromkeys(urls))[:8]

    def _qualified(
        self,
        positive: list[EvidenceItem],
        weak: list[EvidenceItem],
        operations: list[str],
        confidence: float,
        website_status: str,
        crawl_details: str,
        pages_fetched: int,
        extra_reason: str = "",
        disqualifiers: list[str] | None = None,
        all_evidence: list[EvidenceItem] | None = None,
    ) -> ChristmasLightClassificationResult:
        ranked = sorted(positive, key=lambda e: 0 if e.strength == STRONG else 1)
        evidence_terms = list(dict.fromkeys(e.term for e in ranked))
        evidence_urls = list(dict.fromkeys(e.url for e in ranked))
        services = []
        all_items = all_evidence if all_evidence is not None else positive + weak
        christmas, holiday, permanent, roofline = self._service_fields(all_items, thorough=True)
        if christmas == SERVICE_FIELD_YES:
            services.append("Christmas light installation")
        if holiday == SERVICE_FIELD_YES and christmas != SERVICE_FIELD_YES:
            services.append("holiday light installation")
        if permanent == SERVICE_FIELD_YES:
            services.append("permanent decorative lighting installation")
        if roofline == SERVICE_FIELD_YES:
            services.append("roofline lighting installation")
        best = ranked[0]
        strength_note = "explicit service statement" if best.strength == STRONG else "service listing on a dedicated/contextual page"
        reasoning = (
            f"Confirmed {', '.join(services) or 'holiday lighting installation'} ({strength_note}). "
            f"Evidence: \"{best.snippet[:220]}\" ({best.url})."
            f"{extra_reason}"
        )
        return ChristmasLightClassificationResult(
            classification="Yes",
            decision_label="Yes",
            confidence=round(confidence, 3),
            website_status=website_status,
            reasoning=reasoning,
            evidence_phrases=evidence_terms[:8],
            operational_signals=operations[:5],
            disqualifiers=disqualifiers or [],
            christmas_service=christmas,
            holiday_service=holiday,
            permanent_service=permanent,
            roofline_service=roofline,
            evidence_urls=evidence_urls[:6],
            keywords_found=self._keywords(all_items),
            pages_evaluated=pages_fetched,
            crawl_details=crawl_details,
        )

    def _not_qualified(
        self,
        reason: str,
        confidence: float,
        crawl: "CrawlResult",
        crawl_details: str,
        pages_fetched: int,
        evidence: list[EvidenceItem],
        disqualifiers: list[str],
    ) -> ChristmasLightClassificationResult:
        christmas, holiday, permanent, roofline = self._service_fields([], thorough=True)
        return ChristmasLightClassificationResult(
            classification="No",
            decision_label="No",
            confidence=confidence,
            website_status="OK",
            reasoning=reason,
            disqualifiers=disqualifiers,
            christmas_service=christmas,
            holiday_service=holiday,
            permanent_service=permanent,
            roofline_service=roofline,
            evidence_urls=self._inspected_urls(crawl),
            keywords_found=self._keywords(evidence),
            pages_evaluated=pages_fetched,
            crawl_details=crawl_details,
        )

    def _uncertain(
        self,
        reason: str,
        confidence: float,
        crawl: "CrawlResult",
        crawl_details: str,
        pages_fetched: int,
        evidence: list[EvidenceItem],
        thorough: bool,
    ) -> ChristmasLightClassificationResult:
        # A lead under review has no confirmed service: listing-level "Yes" values are reported as Unclear
        fields = [
            SERVICE_FIELD_UNCLEAR if v == SERVICE_FIELD_YES else v
            for v in self._service_fields(evidence, thorough=thorough)
        ]
        christmas, holiday, permanent, roofline = fields
        relevant = [e for e in evidence if e.strength != WEAK or e.is_meaningful_weak]
        weak_urls = list(dict.fromkeys(e.url for e in relevant))
        return ChristmasLightClassificationResult(
            classification="Uncertain",
            decision_label="Uncertain",
            confidence=confidence,
            website_status="OK",
            reasoning=reason,
            christmas_service=christmas,
            holiday_service=holiday,
            permanent_service=permanent,
            roofline_service=roofline,
            evidence_urls=(weak_urls or self._inspected_urls(crawl))[:6],
            keywords_found=self._keywords(evidence),
            pages_evaluated=pages_fetched,
            crawl_details=crawl_details,
        )
