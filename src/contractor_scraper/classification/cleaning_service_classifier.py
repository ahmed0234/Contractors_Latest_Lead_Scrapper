"""Cleaning business qualification + service extraction: is the business a residential cleaning contractor,
which cleaning services does its website actually advertise, and which one is its highest-value service?

The classifier works in three explicit steps, in this order:

1. **Is it a cleaning company?** (`profile_site` + `CleaningServiceClassifier._fuse`). The crawled website
   is profiled once per domain: cleaning services offered, cleaning vocabulary density, residential cues,
   and the signatures of businesses that are *not* targets: directory / listing sites, cleaning-product
   retailers and manufacturers, software / booking platforms, commercial-only janitorial companies, auto
   detailing / car washes, garment dry cleaners, and unrelated trades (roofing, window installation,
   landscaping, ...). The business name and the list's Category / Segment columns are a secondary signal
   (agreement raises confidence; they decide alone only when the site cannot be read, with low confidence).
2. **Which services does it advertise?** Only for confirmed cleaning companies. Service mentions are graded
   per page and in context, never by bare keyword counts:

   - a dedicated page for the service (URL path / page title) is the strongest evidence;
   - a service heading / list item (a short text block) or an offer sentence ("we provide ...",
     "... services", "free estimate") on the homepage or a services page is strong evidence;
   - a plain prose mention is moderate evidence, and only counts when repeated;
   - mentions on blog / news / tips pages, in DIY or "how to" phrasing, in product listings ("carpet
     cleaning machine rental"), after a negation ("we do not offer ..."), or as a task inside a maid
     checklist ("interior window cleaning", "mold and mildew in showers") do not make a service offered;
   - restoration-class services (hoarding, mold, septic, biohazard, fire, water damage, post-construction)
     need strong evidence, so a passing mention never makes them the main service.

   The `Services` value keeps each service's name as the website writes it (headings preferred).
3. **Main Service**: the offered residential service with the best rank in `SERVICE_PRIORITY` (approximate
   average residential job value, highest first). Services that a site only offers to commercial clients
   ("commercial window cleaning for office buildings" with no residential wording) are not eligible.

Verdicts carry a confidence; below `REVIEW_THRESHOLD` a row goes to the uncertain / unreachable sheets while
its Yes/No column is still filled (same convention as the auto repair classifier).
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from contractor_scraper.classification.auto_repair_classifier import downgrade_unusable
from contractor_scraper.normalization.cleaners import clean_company_name

if TYPE_CHECKING:
    from contractor_scraper.discovery.crawler import CrawlResult

CLASSIFIER_VERSION = "2026.11-cleaning-2"

YES = "Yes"
NO = "No"

# Confidence at or above which a verdict is final; below it the lead goes to manual review
REVIEW_THRESHOLD = 0.6
# Below this confidence (or when signals conflict) a row is flagged "borderline" in the reason report
BORDERLINE_THRESHOLD = 0.7

# Evidence score at which a service counts as offered, and the stricter bar for restoration-class services
OFFERED_SCORE = 1.5
STRONG_SCORE = 2.0

# Business types
T_MAID = "Residential Cleaning / Maid Service"
T_EXTERIOR = "Exterior Cleaning / Pressure Washing"
T_CARPET = "Carpet, Upholstery & Floor Cleaning"
T_DUCT = "Air Duct / Dryer Vent / Chimney Cleaning"
T_RESTORATION = "Restoration & Specialty Cleanup"
T_SEPTIC = "Septic Services"
T_CONSTRUCTION = "Post-Construction Cleaning"
T_POOL = "Pool Cleaning"
T_MULTI = "Multi-trade (cleaning among other services)"
T_COMMERCIAL = "Commercial Janitorial (no residential services)"
T_DIRECTORY = "Directory / Listing Site"
T_PRODUCTS = "Cleaning Products Retailer / Manufacturer"
T_SOFTWARE = "Software / Booking Platform"
T_DETAILING = "Auto Detailing / Car Wash"
T_DRY_CLEANER = "Garment Dry Cleaner / Laundry"
T_UNRELATED = "Unrelated Business"
T_UNKNOWN = "Unknown"

EXCLUDED_TYPES = frozenset({T_COMMERCIAL, T_DIRECTORY, T_PRODUCTS, T_SOFTWARE, T_DETAILING, T_DRY_CLEANER, T_UNRELATED})


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives), re.IGNORECASE)


def _norm(text: str) -> str:
    """Lowercase, '&' -> 'and', punctuation -> spaces."""
    text = (text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


# ==================================================================================== service catalog
@dataclass(frozen=True)
class ServiceDef:
    """One cleaning service: how to recognise it on a page and how valuable it is."""

    key: str
    name: str  # canonical name written to "Main Service"
    rank: float | None  # SERVICE_PRIORITY position (1 = highest value); None = never a main service
    patterns: re.Pattern[str]  # name-like phrases; the matched text doubles as the service label
    page_rx: re.Pattern[str]  # URL path / page title of a page dedicated to the service
    group: str  # business type implied when this is the main service
    residential: bool = True  # False = a commercial-only service (janitorial, office cleaning, ...)
    descriptive: re.Pattern[str] | None = None  # descriptive phrases (label = canonical name)
    listing: re.Pattern[str] | None = None  # bare labels accepted only as a short heading / list item
    exclude: re.Pattern[str] | None = None  # context (around the match) that makes a mention not this service
    strict: bool = False  # restoration-class: needs strong evidence
    coordinated: re.Pattern[str] | None = None  # "roof, gutter & window cleaning" (label split per service)
    generic: re.Pattern[str] | None = None  # counts only on maid-type sites (see `_Gen`)
    exclude_before: re.Pattern[str] | None = None  # text right before the match that makes it not an offer


def _svc(key: str, name: str, rank: float | None, group: str, patterns: tuple[str, ...], page: str, **kw: Any) -> ServiceDef:
    for opt in ("descriptive", "listing", "exclude", "exclude_before"):
        if isinstance(kw.get(opt), tuple):
            kw[opt] = _rx(*kw[opt])
        elif isinstance(kw.get(opt), str):
            kw[opt] = re.compile(kw[opt], re.IGNORECASE)
    direct = [p for p in patterns if not isinstance(p, (_Co, _Gen))]
    coordinated = [p for p in patterns if isinstance(p, _Co)]
    generic = [p for p in patterns if isinstance(p, _Gen)]
    if coordinated:
        kw["coordinated"] = _rx(*coordinated)
    if generic:
        kw["generic"] = _rx(*generic)
    return ServiceDef(key, name, rank, _rx(*direct), re.compile(page, re.IGNORECASE), group, **kw)


_ITEM = r"[a-z][a-z'-]*(?:\s+[a-z][a-z'-]*)?"
# Words right before a damage phrase that make it a consequence, not a service ("protects your home from water damage")
CONSEQUENCE_BEFORE = re.compile(
    r"\b(?:prevent\w*|protect\w*|avoid\w*|safeguard\w*|caus\w*|lead\w*\s+to|result\w*\s+in|risk\w*|from|against|costly|"
    r"minimi[sz]\w*|reduc\w*|signs?\s+of|without|any|no)\b[^.|]{0,25}$",
    re.IGNORECASE,
)


class _Co(str):
    """A coordinated-list pattern (matched after the direct patterns; its label is split per service)."""


class _Gen(str):
    """A generic phrase ("deep cleaning", "regular cleaning", "residential cleaning") that only names a maid
    service on a site that actually reads like one (kitchens, bathrooms, dusting, ...)."""


def _gen(pattern: str) -> _Gen:
    return _Gen(pattern)


def _co(noun: str, tail: str) -> _Co:
    """`noun` followed by a coordinated list and then `tail`: "carpet, upholstery & tile cleaning"."""
    return _Co(rf"\b(?:{noun})(?:\s*(?:,|/|\+|&|\band\b|\bor\b)\s*{_ITEM}){{1,4}}\s*,?\s+(?:{tail})\b")

SERVICES: tuple[ServiceDef, ...] = (
    _svc("hoarding", "Hoarding Cleanup", 1, T_RESTORATION,
         (r"\bhoard(?:ing|er|ers)?\s+(?:home\s+|house\s+)?(?:cleaning|clean[\s-]?ups?|clean[\s-]?outs?|cleanouts?|remediation|removal|services?|restoration)\b",
          r"\bhoarder\s+(?:homes?|houses?)\b"),
         r"hoard", listing=r"^hoarding$|^hoarders?$", strict=True),
    _svc("mold", "Mold Remediation", 2, T_RESTORATION,
         (r"\bmou?ld\s+(?:remediation|removal|abatement|clean[\s-]?ups?|cleaning|mitigation|treatment|inspections?|testing|services?)\b",
          r"\bmou?ld\s+(?:and|&)\s+(?:mildew\s+)?(?:remediation|abatement)\b"),
         r"mou?ld",
         exclude=r"\bshowers?\b|\btubs?\b|\bbathtubs?\b|\btoilets?\b|\bsinks?\b|\bcaulk|\bsoap\s+scum|\bgrout\b|\bmildew\s+stains\b|\bdeck\b|\bsiding\b|\broofs?\b|\bdriveway|\bconcrete\b",
         exclude_before=CONSEQUENCE_BEFORE, strict=True),
    _svc("septic", "Septic Tank Cleaning", 3, T_SEPTIC,
         (r"\bseptic\s+(?:tanks?\s+)?(?:pumping|cleaning|clean[\s-]?outs?|services?|maintenance|inspections?)\b", r"\bseptic\s+tanks?\b"),
         r"septic", strict=True),
    _svc("biohazard", "Biohazard Cleaning", 4, T_RESTORATION,
         (r"\bbio[\s-]?hazard\w*\s+(?:cleaning|clean[\s-]?ups?|remediation|removal|services?|decontamination)\b",
          r"\bcrime\s+scene\s+(?:cleaning|clean[\s-]?ups?|services?)\b", r"\btrauma\s+(?:scene\s+)?(?:cleaning|clean[\s-]?ups?)\b",
          r"\bunattended\s+deaths?\b", r"\bblood\s+(?:clean[\s-]?ups?|spill\s+clean\w*)\b", r"\bsuicide\s+clean[\s-]?ups?\b"),
         r"bio-?hazard|crime-?scene|trauma", listing=r"^bio[\s-]?hazard$", strict=True),
    _svc("fire", "Fire and Smoke Damage Cleanup", 5, T_RESTORATION,
         (r"\bfire\s+(?:and|&|/)\s+(?:smoke|water)\s+(?:damage\s+)?(?:restoration|clean[\s-]?ups?|cleaning|repair|mitigation|services?)\b",
          r"\bfire\s+(?:damage\s+)?(?:restoration|clean[\s-]?ups?|cleaning|repair|mitigation|remediation|services?)\b",
          r"\bsmoke\s+(?:and\s+soot\s+)?(?:damage|odor)\s+(?:removal|restoration|clean[\s-]?ups?|cleaning|remediation)\b",
          r"\bsoot\s+(?:removal|clean[\s-]?ups?|cleaning)\b"),
         r"fire[\s_-]|smoke",
         listing=r"^fire\s+(?:(?:and|&)\s+(?:smoke|water)\s+)?damage$|^fire\s+(?:and|&)\s+smoke$|^smoke\s+damage$",
         exclude_before=CONSEQUENCE_BEFORE, strict=True),
    _svc("water", "Water Damage Restoration", 6, T_RESTORATION,
         (r"\bwater\s+damage\s+(?:restoration|clean[\s-]?ups?|cleaning|repair|mitigation|remediation|services?|extraction|removal)\b",
          r"\bflood\s+(?:damage\s+)?(?:clean[\s-]?ups?|restoration|water\s+(?:removal|extraction)|remediation)\b",
          r"(?<!hot\s)(?<!hot-)\bwater\s+(?:extraction|removal|mitigation)\s+(?:services?|and\s+drying)\b", r"\bstructural\s+drying\b",
          r"\bemergency\s+water\s+(?:extraction|removal)\b"),
         r"water[\s_-]damage|flood",
         listing=r"^(?:water|flood)\s+damage$|^water\s+(?:and|&)\s+fire\s+damage$",
         exclude_before=CONSEQUENCE_BEFORE, strict=True),
    _svc("post_construction", "Post-Construction Cleaning", 7, T_CONSTRUCTION,
         (r"\b(?:post|after)[\s-]?construction\s+(?:cleaning|clean[\s-]?ups?|cleans?)\b", r"\bconstruction\s+(?:cleaning|clean[\s-]?ups?)\b",
          r"\bbuilders?\s+clean(?:ing|s)?\b", r"\bnew\s+construction\s+clean\w*", r"\b(?:post|after)[\s-]?(?:renovation|remodel)\s+clean\w*",
          r"\brenovation\s+clean(?:ing|[\s-]?ups?)\b"),
         r"construction|renovation", listing=r"^(?:post|after)[\s-]?construction$", strict=True),
    _svc("roof", "Roof Cleaning", 8, T_EXTERIOR,
         (r"\broofs?\s+(?:cleaning|washing|wash|treatments?|soft[\s-]?wash\w*|moss\s+removal|stain\s+removal|algae\s+removal|cleaners?)\b",
          r"\bmoss\s+(?:removal|treatments?|control|prevention|abatement)\b", r"\bsoft[\s-]?wash(?:ing)?\s+(?:of\s+)?roofs?\b",
          r"\broof\s+moss\b", r"\b(?:algae|black\s+streak)\s+removal\b", _co("roofs?", "cleaning|washing")),
         r"roof|moss", listing=r"^roofs?$"),
    _svc("house_wash", "House Washing", 9, T_EXTERIOR,
         (r"\bhouse\s+(?:washing|wash)\b", r"\bsoft[\s-]?wash(?:ing)?\s+(?:of\s+)?(?:your\s+)?(?:house|home|siding|exterior)s?\b",
          r"\bhome\s+(?:exterior\s+)?washing\b", r"\bexterior\s+(?:house|home)\s+(?:washing|cleaning)\b", r"\bsiding\s+(?:cleaning|washing)\b",
          r"\bexterior\s+(?:washing|wash)\b"),
         r"house-?wash|soft-?wash|siding|exterior-?(?:house|home)",
         # bare "Soft Washing" only as a service heading; "our soft wash method / technique" on a roof page is not house washing
         listing=r"^soft[\s-]?wash(?:ing)?(?:\s+services?)?$",
         exclude=r"\b(?:method|technique|process|approach)\b|\broofs?\b|\bshingles?\b"),
    _svc("air_duct", "Air Duct Cleaning", 10, T_DUCT,
         (r"\b(?:air\s+)?ducts?\s+(?:cleaning|sanitiz\w+|cleaners?)\b", r"\bduct\s*work\s+cleaning\b", r"\bhvac\s+(?:duct\s+|system\s+|air\s+duct\s+)?cleaning\b",
          r"\bair\s+ducts?\b", _co(r"(?:air\s+)?ducts?", "cleaning")),
         r"duct", listing=r"^(?:air\s+)?ducts?$|^(?:air\s+)?vents?\s+cleaning$"),
    _svc("pressure", "Pressure Washing", 11, T_EXTERIOR,
         (r"\bpressure[\s-]?wash(?:ing)?\b", r"\bpower[\s-]?wash(?:ing)?\b", r"\bpressure\s+cleaning\b",
          r"\b(?:driveway|concrete|deck|patio|sidewalk|fence|paver|brick|walkway|flatwork)s?\s+(?:cleaning|washing)\b",
          _co("(?:driveway|concrete|deck|patio|sidewalk|fence|paver|walkway)s?", "cleaning|washing")),
         r"pressure|power-?wash|driveway|concrete|deck|patio"),
    _svc("carpet", "Carpet Cleaning", 12, T_CARPET,
         (r"\bcarpets?\s+(?:cleaning|cleaners?|steam\s+cleaning|shampoo\w*|care|stain\s+removal|restoration)\b",
          r"\bsteam\s+clean(?:ing|ed)?\s+(?:of\s+)?(?:your\s+)?carpets?\b", r"\bhot\s+water\s+extraction\b", _co(r"carpets?", "cleaning|care")),
         r"carpet", listing=r"^carpets?$"),
    _svc("rug", "Area Rug Cleaning", 12.3, T_CARPET,
         (r"\b(?:area\s+|oriental\s+|persian\s+|wool\s+|fine\s+)?rugs?\s+(?:cleaning|washing|care)\b", _co(r"(?:area\s+|oriental\s+)?rugs?", "cleaning")),
         r"rug", listing=r"^(?:area\s+|oriental\s+|persian\s+)?rugs$"),
    _svc("pet_odor", "Pet Stain and Odor Removal", 12.6, T_CARPET,
         (r"\bpet\s+(?:stain|odou?r|urine)s?\s+(?:(?:and|&)\s+odou?r\s+)?(?:removal|treatment|control|remediation)\b", r"\bpet\s+urine\b"),
         r"pet"),
    _svc("tile", "Tile and Grout Cleaning", 13, T_CARPET,
         (r"\btiles?\s*(?:and|&|/|\+)?\s*grout(?:\s+(?:cleaning|sealing|restoration))?\b", r"\bgrout\s+(?:cleaning|sealing|restoration|colou?r\w*)\b",
          r"\btile\s+cleaning\b", _co("tiles?", "cleaning")),
         r"tile|grout", listing=r"^tiles?$"),
    _svc("stone", "Natural Stone Cleaning", 13.2, T_CARPET,
         (r"\b(?:natural\s+)?stone\s+(?:cleaning|polishing|restoration|care|sealing)\b", r"\bmarble\s+(?:cleaning|polishing|restoration|care)\b",
          r"\btravertine\s+(?:cleaning|polishing)\b"),
         r"stone|marble|travertine"),
    _svc("gutter", "Gutter Cleaning", 14, T_EXTERIOR,
         (r"\bgutters?\s+(?:cleaning|clean[\s-]?outs?|cleanouts?|clearing|flushing|brightening|whitening|washing|cleaners?)\b",
          r"\beaves?troughs?\s+cleaning\b", r"\bdownspouts?\s+(?:cleaning|flushing|clearing)\b", _co("gutters?", r"cleaning|clean[\s-]?outs?")),
         r"gutter"),
    _svc("chimney", "Chimney Cleaning", 15, T_DUCT,
         (r"\bchimney\s+(?:sweep\w*|cleaning|cleanings)\b", r"\bfireplace\s+(?:cleaning|sweep\w*)\b"),
         r"chimney|fireplace"),
    _svc("window", "Window Cleaning", 16, T_EXTERIOR,
         (r"\bwindows?\s+(?:cleaning|washing|cleaners?|washers?)\b", _co("windows?", "cleaning|washing")),
         r"window",
         exclude=r"\b(?:interior|inside|indoor)\s+(?:\w+\s+)?windows?\b|\bwindow\s+(?:sills?|tracks?)\b|\bsolution\b|\bsupplies\b"),
    _svc("solar", "Solar Panel Cleaning", 17, T_EXTERIOR,
         (r"\bsolar\s+(?:panels?\s+|arrays?\s+)?(?:cleaning|washing)\b", r"\bsolar\s+panels?\s+(?:maintenance|care)\b"),
         r"solar"),
    _svc("upholstery", "Upholstery Cleaning", 18, T_CARPET,
         (r"\bupholster(?:y|ed\s+furniture)\s+(?:cleaning|care|steam\w*)\b", r"\b(?:sofa|couch|furniture|leather)\s+cleaning\b",
          _co("upholstery", "cleaning|care")),
         r"upholster|furniture|sofa|couch", listing=r"^upholstery$"),
    _svc("mattress", "Mattress Cleaning", 18.5, T_CARPET, (r"\bmattress(?:es)?\s+(?:cleaning|sanitiz\w+)\b",), r"mattress"),
    _svc("move", "Move-In / Move-Out Cleaning", 19, T_MAID,
         (r"\bmove[\s-]?(?:in|out)\b(?:\s*(?:/|and|&|or|-|,)\s*(?:move[\s-]?)?(?:in|out)\b)?\s*(?:cleaning|cleans?|clean[\s-]?ups?|services?)\b",
          r"\bmoving\s+(?:in\s+|out\s+)?clean(?:ing|s)?\b", r"\b(?:end\s+of\s+(?:lease|tenancy)|vacancy|tenant\s+turnover|rental\s+turnover)\s+clean\w*"),
         r"move-?(?:in|out)|moving", listing=r"^move[\s-]?(?:in|out)(?:\s*(?:/|and|&|or|-)\s*(?:move[\s-]?)?(?:in|out))?$"),
    _svc("deep", "Deep House Cleaning", 20, T_MAID,
         (r"\bdeep\s+(?:house|home)\s+clean(?:ing|s)?\b", _gen(r"\bdeep\s+clean(?:ing|s)?\b"), _gen(r"\bspring\s+clean(?:ing)?\b")),
         r"deep-?clean|spring-?clean",
         exclude=r"\bcarpets?\b|\brugs?\b|\bupholster|\bgrout\b|\btiles?\b|\bducts?\b|\bpressure\b|\bsteam\b|\bconcrete\b|\bdriveway|\broofs?\b|\bvents?\b|\bgutters?\b|\bextraction\b"),
    _svc("dryer_vent", "Dryer Vent Cleaning", 21, T_DUCT,
         (r"\bdryer\s+vents?\b(?:\s+(?:cleaning|services?|clean[\s-]?outs?|inspections?))?", r"\bdryer\s+(?:exhaust|duct)\s+cleaning\b"),
         r"dryer", exclude=r"\bdryer\s+vent\s+(?:wizard|squad|superheroes)\b"),
    _svc("house", "House Cleaning", 22, T_MAID,
         (r"\b(?:house|home)\s+clean(?:ing|ers?)\b", r"\bmaid\s+services?\b", _gen(r"\b(?:apartment|condo)\s+clean(?:ing|ers?)\b"),
          r"\b(?:regular|recurring|routine|standard|weekly|bi-?weekly|monthly|basic)\s+(?:house|home|maid)\s+clean(?:ing|s)\b",
          r"\bhousekeeping(?:\s+services?)?\b", r"\bhousecleaning\b",
          _gen(r"\bresidential\s+clean(?:ing|ers?)\b"),
          _gen(r"\b(?:regular|recurring|routine|standard|weekly|bi-?weekly|monthly|basic)\s+clean(?:ing|s)\b")),
         r"house-?clean|home-?clean|residential|maid|housekeep|recurring|standard-?clean|regular-?clean",
         descriptive=(r"\bclean(?:ing)?\s+(?:services?\s+)?for\s+(?:your\s+)?(?:homes?|houses?|residences?|homeowners?|families)\b",
                      r"\bresidential\s+(?:and|&|/)\s+commercial\s+cleaning\b", r"\bclean(?:ing)?\s+your\s+(?:home|house)\b"),
         listing=r"^(?:maid\s+service|maids?|house\s*keeping)$",
         exclude=r"\bin[\s-]home\b|\bfront[\s-]+of[\s-]+(?:the[\s-]+)?house\b"),
    _svc("blinds", "Blind and Shutter Cleaning", 23, T_MAID,
         (r"\bblinds?\s+(?:cleaning|washing)\b", r"\bshutters?\s+cleaning\b", r"\b(?:drapery|drapes|curtains?)\s+cleaning\b",
          _co("blinds?", "cleaning")),
         r"blind|shutter|drape"),
    _svc("pool", "Pool Cleaning", 24, T_POOL,
         (r"\b(?:swimming\s+)?pool\s+(?:cleaning|services?|maintenance|care|cleaners?)\b", r"\bpool\s+(?:and|&)\s+spa\s+(?:services?|cleaning|maintenance)\b"),
         r"pool", exclude=r"\bpool\s+tables?\b|\bcar\s*pool"),
    _svc("trash_can", "Trash Can Cleaning", 25, T_EXTERIOR,
         (r"\b(?:trash|garbage|waste|recycl\w+)\s*(?:cans?|bins?|carts?|containers?)\s+(?:cleaning|washing|sanitiz\w+|cleaners?)\b",
          r"\bbins?\s+(?:cleaning|washing)\b", r"\bcan\s+(?:cleaning|washing)\s+(?:services?|business|company)\b(?!\s+be\b)"),
         r"trash|garbage|bin-?clean|can-?clean"),
    # ---- residential services outside the priority list: main service only when nothing ranked is offered
    _svc("hardwood", "Hard Floor Cleaning", 13.5, T_CARPET,
         (r"\bhard(?:wood)?\s+(?:floors?\s+|surface\s+)?(?:cleaning|refinishing|care)\b", r"\bwood\s+floors?\s+cleaning\b",
          r"\b(?:laminate|vinyl|lvp)\s+(?:floors?\s+)?cleaning\b"),
         r"hardwood|hard-?floor|wood-?floor"),
    _svc("vacation", "Vacation Rental / Airbnb Cleaning", 22.5, T_MAID,
         (r"\b(?:airbnb|vrbo|vacation\s+rentals?|short[\s-]?term\s+rentals?)\s+(?:cleaning|turnovers?|clean\w*|services?)\b",),
         r"airbnb|vacation|short-?term", listing=r"^airbnbs?$|^vacation\s+rentals?$"),
    _svc("grill", "Grill Cleaning", 25.5, T_EXTERIOR, (r"\b(?:bbq|barbecue|grill)\s+cleaning\b",), r"grill|bbq"),
    _svc("oven", "Oven Cleaning", 25.5, T_MAID, (r"\boven\s+cleaning\b",), r"oven"),
    # ---- commercial / other cleaning services: listed in Services, never the residential main service
    _svc("commercial", "Commercial Cleaning", None, T_COMMERCIAL,
         (r"\bcommercial\s+(?:and\s+residential\s+)?(?:cleaning|cleaners?)\b", r"\bbusiness\s+cleaning\b"),
         r"commercial", residential=False),
    _svc("office", "Office Cleaning", None, T_COMMERCIAL,
         (r"\boffice\s+(?:building\s+)?cleaning\b", r"\b(?:medical|dental)\s+(?:office|facility)\s+cleaning\b"), r"office", residential=False),
    _svc("janitorial", "Janitorial Services", None, T_COMMERCIAL,
         (r"\bjanitorial\b(?:\s+services?)?", r"\bcustodial\b(?:\s+services?)?", r"\bday\s+porter\b", r"\bfacility\s+(?:cleaning|maintenance)\b",
          r"\bbuilding\s+maintenance\b"),
         r"janitor|custodial|porter|facility", residential=False),
    _svc("floor_care", "Floor Stripping and Waxing", None, T_COMMERCIAL,
         (r"\bfloor\s+(?:stripping|waxing|burnishing|buffing|scrubbing|finishing)\b", r"\bstrip(?:ping)?\s+(?:and|&)\s+wax(?:ing)?\b"),
         r"floor-?care|strip", residential=False),
    _svc("industrial", "Industrial Cleaning", None, T_COMMERCIAL,
         (r"\bindustrial\s+cleaning\b", r"\b(?:kitchen\s+)?(?:exhaust\s+)?hood\s+cleaning\b", r"\bparking\s+lot\s+sweeping\b",
          r"\bwarehouse\s+cleaning\b", r"\bfleet\s+washing\b",
          r"\b(?:street|power|lot|parking\s+(?:lot|garage)|construction\s+site)\s+sweeping\b", r"\bsweeping\s+services?\b"),
         r"industrial|hood|sweeping|fleet", residential=False),
    _svc("disinfection", "Disinfection & Sanitizing", None, T_MAID,
         (r"\b(?:disinfect\w*|saniti[sz]\w*|electrostatic)\s+(?:services?|spraying|cleaning)\b",), r"disinfect|saniti", residential=False),
)
SERVICE_BY_KEY: dict[str, ServiceDef] = {s.key: s for s in SERVICES}
# The approximate average residential job value order the main service is chosen by (highest first)
SERVICE_PRIORITY: tuple[str, ...] = tuple(
    s.key for s in sorted((s for s in SERVICES if s.rank is not None), key=lambda s: s.rank or 0)
)
COMMERCIAL_KEYS = frozenset(s.key for s in SERVICES if not s.residential and s.group == T_COMMERCIAL)

# Cheap prefilter: a text segment that matches none of these stems cannot name a cleaning service
_SERVICE_STEMS = re.compile(
    r"clean|wash|sweep|maid|mou?ld|septic|hoard|hazard|crime|trauma|fire|smoke|soot|water|flood|construct|renovat|moss|algae|"
    r"duct|vent|chimney|fireplace|carpet|rug|upholst|sofa|couch|mattress|leather|grout|tile|stone|marble|gutter|eaves|downspout|"
    r"window|solar|pool|trash|garbage|bin|blind|shutter|drape|curtain|janitor|custodial|porter|housekeep|move|moving|deep|spring|"
    r"airbnb|vrbo|vacation|rental|tenant|floor|hardwood|strip|wax|hood|grill|bbq|oven|disinfect|saniti|electrostatic|extraction|"
    r"odor|pet|residential|commercial|office|industrial|facility|building|siding|driveway|concrete|deck|patio|sidewalk|fence|paver|"
    r"surface|steam|shampoo|roof|unattended|blood|suicide|exterior|drying|hvac|air|septic|death|recurring|regular|weekly|apartment|"
    r"condo|house|home|business|warehouse|parking|biohazard",
    re.IGNORECASE,
)

# =============================================================================== context grading
OFFER_CONTEXT = re.compile(
    r"\b(?:we|our\s+(?:team|company|crew|technicians?|cleaners?|staff|pros|experts?))\s+(?:also\s+)?(?:offer|provide|specializ\w+|perform|do|handle|"
    r"are\s+(?:experts?|specialists?)|deliver|bring)|\bservices?\s+(?:include|offered|we\s+offer)\b|\bspeciali[sz]\w*\s+in\b|"
    r"\b(?:professional|expert|certified|licensed|insured|affordable|reliable|trusted|local|top[\s-]rated|best)\b|\bservices?\b|"
    r"\bcall\s+(?:us|today|now)\b|\bfree\s+(?:estimates?|quotes?|inspections?)\b|\b(?:book|schedule|request)\b|\bquotes?\b|\bpricing\b|"
    r"\bstarting\s+at\b|\bavailable\b|\bwe\s+are\s+(?:a|the|your)\b|\byour\s+(?:home|house|property|business)\b",
    re.IGNORECASE,
)
NEGATION = re.compile(
    r"\b(?:not|don'?t|do\s+not|does\s+not|doesn'?t|never|nor|no\s+longer|except|excluding|without|unable\s+to|cannot|can'?t)\b[^.|]{0,40}$",
    re.IGNORECASE,
)
DIY_CONTEXT = re.compile(
    r"\bhow\s+(?:to|often|much|long)\b|\btips?\b|\bdiy\b|\byourself\b|\bshould\s+you\b|\bsigns?\s+(?:that\s+)?(?:you|your)\b|\bwhy\s+(?:you|is|are|does)\b|"
    r"\bwhat\s+(?:is|are|to\s+know)\b|\bguide\b|\bmyths?\b|\bmistakes?\b|\bcost\s+of\b|\bbenefits\s+of\b|\breasons?\s+(?:to|why)\b|\bvs\.?\b|\bversus\b|"
    r"\bfaqs?\b|\bquestions?\b|\bread\s+more\b|\bposted\s+(?:on|in|by)\b|\bblog\b",
    re.IGNORECASE,
)
RETAIL_CONTEXT = re.compile(
    r"\badd\s+to\s+cart\b|\bbuy\s+now\b|\bshop\s+(?:now|our)\b|\bsku\b|\bin\s+stock\b|\bout\s+of\s+stock\b|\bmachines?\b|\bequipment\b|"
    r"\bsolutions?\b|\bchemicals?\b|\bdetergents?\b|\bproducts?\b|\bsupplies\b|\b(?:machine|equipment)\s+rentals?\b|\brent\s+(?:a|our)\b|"
    r"\bwand\b|\bnozzles?\b|\bfor\s+sale\b",
    re.IGNORECASE,
)
# A mention preceded by these words is a service offered to businesses, not households
COMMERCIAL_PREFIX = re.compile(
    r"\b(?:commercial|office|industrial|corporate|janitorial|business(?:es)?|restaurants?|medical|retail|warehouses?|high[\s-]rise|storefronts?|"
    r"property\s+managers?|hoa|municipal)\b[^.|]{0,25}$",
    re.IGNORECASE,
)
RESIDENTIAL_PREFIX = re.compile(r"\b(?:residential|home|house|homeowners?|apartment|condo)\b[^.|]{0,25}$", re.IGNORECASE)
COMMERCIAL_PAGE_CUE = re.compile(
    r"\bcommercial\b|\bbusiness(?:es)?\b|\boffices?\b|\bcontractors?\b|\bbuilders?\b|\bfacilit(?:y|ies)\b|\bproperty\s+managers?\b|"
    r"\bretail\b|\bwarehouses?\b|\bindustrial\b|\bcorporate\b|\bschools?\b|\bmedical\b|\bgeneral\s+contractors?\b",
    re.IGNORECASE,
)
# Segments that are a maid's task checklist: a service word inside is a task, not a separate service
CHECKLIST_WORDS = re.compile(
    r"\b(?:baseboards?|cabinets?|appliances?|dust(?:ing)?|vacuum(?:ing)?|mop(?:ping)?|sinks?|toilets?|mirrors?|ovens?|fridges?|refrigerators?|"
    r"microwaves?|countertops?|ceiling\s+fans?|light\s+fixtures?|beds?|linens?|trash|showers?|tubs?|stovetops?|sills?|doorknobs?)\b",
    re.IGNORECASE,
)
TASK_SERVICES = frozenset({"window", "blinds", "upholstery", "carpet", "oven", "tile", "mold"})

RESIDENTIAL_CUE = re.compile(
    r"\bresidential\b|\bhomeowners?\b|\byour\s+(?:home|house|family)\b|\b(?:home|house)\s+clean\w*|\bhomes\b|\bhouses\b|\bapartments?\b|"
    r"\bcondos?\b|\bmaids?\b|\bfamilies\b|\bresidences?\b",
    re.IGNORECASE,
)
COMMERCIAL_ONLY_STATEMENT = re.compile(
    r"\b(?:commercial|janitorial)\s+(?:cleaning\s+)?(?:services?\s+)?only\b|\bexclusively\s+(?:commercial|for\s+businesses|serving\s+businesses)\b|"
    r"\bwe\s+(?:do\s+not|don'?t)\s+(?:offer|provide|do)\s+residential\b|\bno\s+residential\b|\bonly\s+(?:serve|service|clean)\s+(?:commercial|businesses)\b",
    re.IGNORECASE,
)
CLEANING_VOCAB = re.compile(
    r"\b(?:clean\w*|maids?|janitorial|custodial|housekeep\w*|saniti[sz]\w*|disinfect\w*|pressure\s+wash\w*|power\s+wash\w*|soft\s*wash\w*|"
    r"washing|steam\w*|sweep\w*|scrub\w*|spotless|dusting|vacuum\w*|mopping|squeegee|remediation|restoration|mou?ld|ducts?|upholstery|grout|gutters?)\b",
    re.IGNORECASE,
)
# Homepage title / description words that make the business a cleaning business (not "Replacement Windows" or "Gutters")
CLEANING_HEAD = re.compile(
    r"\bclean\w*|\bmaids?\b|\bjanitorial\b|\bcustodial\b|\bhousekeep\w*|\bpressure\s*wash\w*|\bpower\s*wash\w*|\bsoft\s*wash\w*|"
    r"\bhouse\s*wash\w*|\bwindow\s+wash\w*|\bsweeps?\b|\bsteam\w*|\bremediation\b|\bupholstery\b|\bgrout\b|\bsepti[ck]\s+pump\w*|"
    r"\bhoard\w*|\bbiohazard\b|\bwater\s+damage\b",
    re.IGNORECASE,
)
# Description-style "house cleaning" phrases inside another service's sentence ("cleaning your home's carpets")
HOUSE_DESCRIPTIVE_EXCLUDE = re.compile(
    r"\bcarpets?\b|\brugs?\b|\bupholster|\bducts?\b|\bwindows?\b|\bgutters?\b|\broofs?\b|\btile\b|\bgrout\b|\bpressure\b|\bsiding\b|"
    r"\bexterior\b|\bvents?\b|\bair\s+quality\b|\bwash",
    re.IGNORECASE,
)
# Another business recommended or partnered with ("Need house cleaning? Try ...", "we partner with ...")
REFERRAL_CONTEXT = re.compile(
    r"\bneed\s+[^?|]{0,40}\?\s*(?:try|call|visit|check\s+out)\b|\bwe\s+(?:recommend|partner\s+with|refer)\b|\breferral\s+partners?\b|"
    r"\bsister\s+compan(?:y|ies)\b|\bour\s+(?:partners?|friends)\s+at\b|\bpowered\s+by\b",
    re.IGNORECASE,
)
# Homepage title / description of a business that serves commercial clients (and does not mention households)
COMMERCIAL_HEAD = re.compile(r"\bcommercial\b|\bjanitorial\b|\bjanitor\b|\bcustodial\b|\bfacilit(?:y|ies)\b|\boffice\s+clean\w*|\bbuilding\s+(?:maintenance|services)\b", re.IGNORECASE)
RESIDENTIAL_HEAD = re.compile(
    r"\bresidential\b|\bhomes?\b|\bhouses?\b|\bmaids?\b|\bhousekeep\w*|\bcarpet\b|\bupholstery\b|\bwindows?\b|\bgutters?\b|\broofs?\b|"
    r"\bpressure\b|\bsoft\s*wash|\bducts?\b|\bdryer\b|\bchimney\b|\bmove[\s-]?(?:in|out)\b|\bsolar\b|\brestoration\b",
    re.IGNORECASE,
)
DRY_CLEANER_HEAD = re.compile(r"\bdry\s+clean(?:ing|ers?)\b|\blaundr\w+\b|\blaundromat\b|\btailor\w*\b|\balterations\b", re.IGNORECASE)
DETAILING_HEAD = re.compile(r"\bdetail(?:ing|ers?)?\b|\bcar\s+wash\b|\bauto\s+spa\b|\bceramic\s+coating\b", re.IGNORECASE)
# A short text block with these words is a sentence fragment ("High-pressure washing can:"), not a heading
SENTENCE_WORDS = re.compile(r"\b(?:can|could|may|might|will|would|should|is|are|was|were|isn'?t|aren'?t|does|do)\b", re.IGNORECASE)
# Contrast / warning wording around a mention: the service is discussed, not offered
CONTRAST_CONTEXT = re.compile(
    r"\binstead\s+of\b|\brather\s+than\b|\bunlike\b|\bmyths?\b|\bmisconceptions?\b|\bcan\s+(?:damage|ruin|strip|harm)\b|\bdamaging\b|"
    r"\bvoids?\s+(?:the|your)\s+warranty\b|\bharmful\b|\bharsh\b|\bwe\s+never\b|\bnot\s+recommended\b",
    re.IGNORECASE,
)
# Maid-service vocabulary (a site with several of these reads like a house-cleaning business)
MAID_VOCAB = re.compile(
    r"\b(?:kitchens?|bathrooms?|bedrooms?|dusting|baseboards?|mopping|vacuuming|maids?|housekeep\w*|laundry|linens?|countertops?|"
    r"toilets?|showers?|mirrors?|cabinets?|ceiling\s+fans?|appliances?|stovetops?|microwaves?|refrigerators?|fridges?|ovens?)\b",
    re.IGNORECASE,
)
MAID_HEAD = re.compile(r"\bmaids?\b|\bhouse\s*clean\w*|\bhome\s+clean\w*|\bhousekeep\w*|\bresidential\s+clean\w*", re.IGNORECASE)
# Short headings that open an add-on / extras menu: the items after them are extras, not main services
ADDON_HEADING = re.compile(r"\badd[\s-]?ons?\b|\bextras?\b|\bupgrades?\b|\boptional\b|\bà\s+la\s+carte\b", re.IGNORECASE)

# ======================================================================================= exclusions
DIRECTORY_DOMAINS: frozenset[str] = frozenset({
    "yelp.com", "angi.com", "angieslist.com", "homeadvisor.com", "thumbtack.com", "bbb.org", "yellowpages.com", "porch.com", "houzz.com",
    "nextdoor.com", "mapquest.com", "manta.com", "chamberofcommerce.com", "care.com", "handy.com", "taskrabbit.com", "bark.com",
    "networx.com", "expertise.com", "threebestrated.com", "superpages.com", "local.com", "citysearch.com", "merchantcircle.com",
    "birdeye.com", "alignable.com", "buildzoom.com", "homeguide.com", "fixr.com", "google.com", "maps.google.com", "bing.com",
    "facebook.com", "m.facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "x.com", "twitter.com", "youtube.com",
})
DIRECTORY_PATTERNS = _rx(
    r"\b(?:find|compare|hire)\s+(?:the\s+)?(?:best|top|local|trusted|top-rated)\s+(?:\w+\s+){0,3}(?:pros|professionals|companies|contractors|cleaners|services|near\s+you)\b",
    r"\bget\s+(?:free\s+)?(?:quotes?|estimates?)\s+from\s+(?:multiple|local|up\s+to|several|top)\b",
    r"\btop\s+\d+\s+(?:best\s+)?(?:\w+\s+){0,3}(?:companies|services|cleaners|pros)\b", r"\bclaim\s+(?:this|your)\s+(?:business|listing)\b",
    r"\bwrite\s+a\s+review\b", r"\bsponsored\s+(?:listings?|results?)\b", r"\bbusiness\s+directory\b", r"\blist\s+your\s+business\b",
    r"\bget\s+matched\s+with\b", r"\bfor\s+pros\b", r"\bjoin\s+(?:as\s+a|our\s+network\s+of)\s+pros?\b",
)
SOFTWARE_PATTERNS = _rx(
    r"\bsoftware\b", r"\bsaas\b", r"\bcrm\b", r"\bfree\s+trial\b", r"\b(?:request|book|schedule|watch)\s+a\s+demo\b", r"\bintegrations?\b",
    r"\bapi\b", r"\bper\s+(?:user|month|seat)\b", r"\bpricing\s+plans?\b", r"\bfor\s+cleaning\s+(?:businesses|companies|pros)\b",
    r"\bgrow\s+your\s+cleaning\s+business\b", r"\b(?:scheduling|invoicing|dispatch(?:ing)?|route\s+optimization)\s+(?:software|app|tool)s?\b",
)
SOFTWARE_HEAD = re.compile(r"\bsoftware\b|\bapp\b|\bplatform\b|\bcrm\b|\bsaas\b|\bfor\s+cleaning\s+(?:businesses|companies)\b", re.IGNORECASE)
# Retail / manufacturing signals. Prices, carts and checkouts alone are not counted: service companies sell
# subscriptions and bookings online too (trash-can cleaning plans, maid packages).
COMMERCE_PATTERNS = _rx(
    r"\badd\s+to\s+cart\b", r"\bshop\s+now\b", r"\bfree\s+shipping\b", r"\b(?:our|shop|browse|view)\s+(?:all\s+)?products\b",
    r"\bcleaning\s+(?:supplies|chemicals|equipment)\b", r"\bjanitorial\s+(?:supplies|products|equipment)\b", r"\bwholesale\b",
    r"\bdistributors?\b", r"\bmanufacturers?\s+of\b", r"\bin\s+stock\b", r"\bsku\b", r"\bproduct\s+catalog\b", r"\bdealer\s+locator\b",
    r"\bwhere\s+to\s+buy\b", r"\bships?\s+(?:free|within|same\s+day)\b", r"\bbulk\s+(?:orders?|pricing)\b",
)
PRODUCTS_HEAD = re.compile(
    r"\bsupplies\b|\bproducts\b|\bequipment\b|\bwholesale\b|\bdistribut\w+\b|\bmanufactur\w+\b|\bstore\b|\bshop\b|\bchemicals?\b", re.IGNORECASE
)
DETAILING_PATTERNS = _rx(
    r"\b(?:auto|car|mobile|vehicle|boat|rv)\s+detail\w*\b", r"\bcar\s+wash\w*\b", r"\bceramic\s+coat\w*\b", r"\bpaint\s+correction\b",
    r"\bwax\s+and\s+polish\b", r"\binterior\s+(?:car\s+)?detail\w*\b",
)
DRY_CLEANER_PATTERNS = _rx(
    r"\bdry\s+clean(?:ing|ers?)\b(?![^|.]{0,20}\bcarpet)", r"\blaundromat\b", r"\bwash\s+(?:and|&)\s+fold\b", r"\balterations?\b",
    r"\btailor\w*\b", r"\bgarment\s+care\b", r"\bshirts?\s+(?:laundered|pressed)\b",
)
UNRELATED_INDUSTRIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("window/door installation company", _rx(r"\b(?:replacement|new|vinyl)\s+windows\b", r"\bwindows?\s+(?:installation|replacement|installers?)\b", r"\bentry\s+doors?\b", r"\bpatio\s+doors\b")),
    ("roofing contractor", _rx(r"\broofing\b", r"\broof\s+(?:replacement|repairs?|installation|inspections?)\b", r"\bshingles?\b")),
    ("painting contractor", _rx(r"\b(?:interior|exterior)\s+painting\b", r"\bpainters?\b", r"\bpainting\s+(?:contractor|company|services)\b")),
    ("landscaping / lawn care company", _rx(r"\blandscap\w+\b", r"\blawn\s+(?:care|mowing|maintenance)\b", r"\bmowing\b", r"\bhardscap\w+\b", r"\btree\s+(?:service|trimming|removal)\b")),
    ("snow removal company", _rx(r"\bsnow\s+(?:removal|plowing|plow)\b", r"\bplowing\b", r"\bde-?icing\b")),
    ("HVAC contractor", _rx(r"\bhvac\b", r"\bfurnace\s+(?:repair|installation|replacement)\b", r"\bac\s+(?:repair|installation)\b", r"\bheating\s+(?:and|&)\s+(?:cooling|air)\b")),
    ("plumbing / electrical contractor", _rx(r"\bplumb\w+\b", r"\bwater\s+heaters?\b", r"\belectricians?\b", r"\belectrical\s+(?:contractor|services)\b")),
    ("flooring / construction company", _rx(r"\bflooring\s+(?:installation|store|contractor|sales)\b", r"\bcarpet\s+installation\b", r"\bremodel\w*\b", r"\bgeneral\s+contractor\b", r"\bhome\s+builders?\b", r"\bconstruction\s+company\b")),
    ("pest control company", _rx(r"\bpest\s+control\b", r"\bexterminat\w+\b", r"\btermites?\b")),
    ("moving / junk removal company", _rx(r"\bmoving\s+(?:company|services)\b", r"\bmovers\b", r"\bjunk\s+(?:removal|hauling)\b", r"\bdumpster\s+rentals?\b")),
    ("real estate / property management business", _rx(r"\breal\s+estate\b", r"\brealtors?\b", r"\bproperty\s+management\b", r"\bhomes\s+for\s+sale\b")),
    ("staffing agency", _rx(r"\bstaffing\b", r"\brecruit\w+\b", r"\bjob\s+seekers\b")),
    ("IT / marketing / web design company", _rx(r"\bweb\s+(?:design|hosting|development)\b", r"\bdigital\s+marketing\b", r"\bseo\s+services\b", r"\bmanaged\s+it\b")),
    ("accounting / legal / financial firm", _rx(r"\baccounting\b", r"\btax\s+(?:preparation|services)\b", r"\battorneys?\b", r"\blaw\s+firm\b", r"\binsurance\s+agency\b", r"\bmortgage\b")),
    ("healthcare provider", _rx(r"\bpatients?\b", r"\bdental\s+(?:care|office)\b", r"\bphysicians?\b", r"\bclinic\b")),
    ("restaurant / food business", _rx(r"\bmenu\b", r"\brestaurant\b", r"\bcatering\b", r"\border\s+online\b")),
)

# Hosts that are links (map shares, booking forms, chat) rather than the business's website
NON_SITE_HOSTS: frozenset[str] = frozenset({
    "share.google", "g.co", "goo.gl", "maps.app.goo.gl", "maps.google.com", "google.com", "wa.me", "api.whatsapp.com", "form.jotform.com",
    "clienthub.getjobber.com", "book.housecallpro.com", "squareup.com", "square.site", "calendar.app.google", "calendly.com", "forms.gle",
    "docs.google.com", "linktr.ee", "bit.ly", "tinyurl.com", "booksy.com", "app.squarespacescheduling.com", "app.acuityscheduling.com",
    # link-in-bio profiles: a list of links, not the business's website (and crawling them reaches the platform's own site)
    "brand.page", "beacons.ai", "linkin.bio", "taplink.cc", "bio.link", "campsite.bio", "solo.to", "msha.ke", "lnk.bio",
})

EDITORIAL_PAGE = re.compile(r"/(?:blog|news|articles?|posts?|press|tips|resources|learn|guides?|\d{4}/\d{2})(?:/|$)", re.IGNORECASE)
# Article-style page titles ("Roof Cleaning for Insurance: What Homeowners Need to Know", "5 Signs You Need ...")
ARTICLE_TITLE = re.compile(
    r"\bwhat\s+(?:\w+\s+){0,2}(?:need|needs|should)\s+to\s+know\b|\bhow\s+to\b|\bhow\s+often\b|\bwhy\s+(?:you|your|is|are|do|does|should)\b|"
    r"\b\d+\s+(?:signs|tips|reasons|ways|mistakes|things|benefits)\b|\bguide\s+to\b|\bvs\.?\b|\bversus\b|\?\s*(?:\||$)|\bdiy\b",
    re.IGNORECASE,
)
SERVICE_PAGE = re.compile(r"service|clean|wash|maid|residential|commercial|what-we-do|pricing|price|rates|packages|offer|restor|remediat", re.IGNORECASE)
INFO_PAGE = re.compile(r"about|contact|faq|area|location|review|testimonial|gallery|team|why|who-we-are|our-story", re.IGNORECASE)
ARCHIVE_PATH_PREFIX = re.compile(r"^/web/\d+[a-z_]*/(?:https?:/+[^/]+)?", re.IGNORECASE)
LABEL_GENERIC = frozenset({
    "services", "service", "professional", "expert", "our", "the", "best", "quality", "affordable", "premium", "top", "local", "and",
    "a", "of", "your", "we", "offer", "provide", "complete", "full", "experts",
})
# Words written right before a service name that belong to it ("Residential Carpet Cleaning")
LABEL_MODIFIERS = frozenset({
    "residential", "commercial", "exterior", "interior", "emergency", "eco-friendly", "green", "steam", "deep", "low-pressure",
    "organic", "pet-friendly", "weekly", "bi-weekly", "biweekly", "monthly", "recurring", "one-time", "post-construction",
})
# Last word of a coordinated match that names the work ("... Window Cleaning")
LABEL_TAIL = re.compile(r"(cleaning|washing|wash|care|clean[\s-]?outs?|cleanouts?)$", re.IGNORECASE)
LIST_SEPARATOR = re.compile(r"\s*(?:,|/|\+|&|\band\b|\bor\b)\s*", re.IGNORECASE)


# ============================================================================================ results
@dataclass
class ServiceHit:
    key: str
    label: str  # the service as written on the page
    url: str
    kind: str  # page | listing | statement | mention
    score: float
    snippet: str
    commercial: bool = False  # offered to commercial clients only ("commercial window cleaning")
    addon: bool = False  # listed under an add-on / extras heading
    generic: bool = False  # generic maid phrase (counts only on maid-type sites)
    template: bool = False  # in navigation / footer text repeated across the site


@dataclass
class ServiceEvidence:
    key: str
    hits: list[ServiceHit] = field(default_factory=list)
    score: float = 0.0
    residential_score: float = 0.0

    @property
    def definition(self) -> ServiceDef:
        return SERVICE_BY_KEY[self.key]

    @property
    def best(self) -> ServiceHit:
        return max(self.hits, key=lambda h: h.score)

    @property
    def offered(self) -> bool:
        return self.score >= (STRONG_SCORE if self.definition.strict else OFFERED_SCORE)

    @property
    def residential_offered(self) -> bool:
        need = STRONG_SCORE if self.definition.strict else OFFERED_SCORE
        return self.definition.residential and self.residential_score >= need

    def label(self) -> str:
        """The name the site uses for this service: from its strongest presentation (heading / dedicated
        page first), preferring names that end in the work itself ("Carpet Cleaning" over "Carpet Care Tips")."""
        order = {"listing": 0, "page": 1, "statement": 2, "mention": 3}
        usable = [h for h in self.hits if h.score >= 0.5] or self.hits

        def quality(h: ServiceHit) -> tuple[int, int, int, float]:
            ends_well = bool(re.search(r"(?:cleaning|washing|wash|removal|remediation|restoration|clean[\s-]?ups?|cleanouts?|pumping|sweep\w*|care|services?|polishing)$", h.label, re.I))
            return (0 if ends_well else 1, order.get(h.kind, 4), len(h.label.split()), -h.score)

        return min(usable, key=quality).label


@dataclass
class SiteProfile:
    """What a crawled website says about the business (independent of any input row)."""

    accessible: bool
    pages: int = 0
    word_count: int = 0
    cleaning_mentions: int = 0
    residential_cues: int = 0
    commercial_only_statement: str = ""
    services: dict[str, ServiceEvidence] = field(default_factory=dict)
    directory_score: float = 0.0
    software_score: float = 0.0
    commerce_score: float = 0.0
    maid_words: int = 0
    detailing_score: float = 0.0
    dry_cleaner_score: float = 0.0
    industry: str = ""
    home_head: str = ""
    site_type: str = T_UNKNOWN
    verdict: str = ""  # Yes | No | "" (site gives no verdict)
    strength: float = 0.0
    summary: str = ""
    main_service: str = ""  # key
    excluded_evidence: list[str] = field(default_factory=list)

    @property
    def offered(self) -> list[ServiceEvidence]:
        return [e for e in self.services.values() if e.offered]

    @property
    def residential_ranked(self) -> list[ServiceEvidence]:
        """Offered residential services, highest value first."""
        items = [e for e in self.services.values() if e.residential_offered and e.definition.rank is not None]
        return sorted(items, key=lambda e: (e.definition.rank or 99, -e.score))

    @property
    def commercial_offered(self) -> list[ServiceEvidence]:
        return [e for e in self.offered if e.key in COMMERCIAL_KEYS]

    @property
    def cleaning_dense(self) -> bool:
        """Cleaning is a real theme of the site, not a passing mention."""
        if self.cleaning_mentions >= 25:
            return True
        return self.cleaning_mentions >= 5 and self.cleaning_mentions * 1000 >= 6 * max(1, self.word_count)

    @property
    def head_is_cleaning(self) -> bool:
        return bool(CLEANING_HEAD.search(self.home_head))

    def service_labels(self) -> list[str]:
        """Every offered service as written on the site, highest-value first (commercial/other last)."""
        def order(e: ServiceEvidence) -> tuple[float, float]:
            rank = e.definition.rank
            return (rank if rank is not None and e.residential_offered else 100.0, -e.score)

        labels: list[str] = []
        seen: set[str] = set()
        for ev in sorted(self.offered, key=order):
            label = ev.label()
            key = _label_key(label) or ev.key
            if key not in seen:
                seen.add(key)
                labels.append(label)
        return labels[:20]

    def evidence_urls(self, limit: int = 4) -> list[str]:
        if self.verdict == YES:
            hits = [h for e in self.residential_ranked for h in sorted(e.hits, key=lambda h: -h.score)[:1]]
        elif self.site_type == T_COMMERCIAL:
            hits = [h for e in self.commercial_offered for h in sorted(e.hits, key=lambda h: -h.score)[:1]]
        else:
            return list(dict.fromkeys(self.excluded_evidence))[:limit]
        return list(dict.fromkeys(h.url for h in hits if h.url))[:limit]


@dataclass
class CleaningClassificationResult:
    """Verdict for one lead: is it a residential cleaning company, which services, which main service?"""

    classification: str  # output bucket: Yes | No | Uncertain (needs review) | Unreachable
    decision_label: str  # Yes | No (always decided; written to "Is Cleaning Company?")
    confidence: float
    website_status: str
    main_service: str = ""  # canonical name of the highest-value offered residential service
    services: list[str] = field(default_factory=list)  # every offered service, as written on the website
    reasoning: str = ""
    short_reason: str = ""
    business_type: str = T_UNKNOWN
    decision_basis: str = ""
    evidence_urls: list[str] = field(default_factory=list)
    evidence_phrases: list[str] = field(default_factory=list)
    access_error: str = ""
    pages_evaluated: int = 0
    crawl_details: str = ""
    site_accessible: bool = False

    @property
    def qualification_status(self) -> str:
        return {"Yes": "Qualified", "No": "Rejected", "Uncertain": "Needs Review"}.get(self.classification, "Unverified")

    @property
    def borderline(self) -> bool:
        return self.confidence < BORDERLINE_THRESHOLD or any(w in self.decision_basis for w in ("conflict", "disagree"))

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CleaningClassificationResult":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class LeadInfo:
    """The row values the classifier uses besides the website."""

    business_name: str = ""
    category: str = ""  # e.g. Overture category "Carpet Cleaning"
    segment: str = ""  # e.g. "Residential / Maid"
    website: str = ""  # the website cell as given (explains missing / non-site links)


# ============================================================================================ helpers
def _page_path(url: str) -> str:
    path = urlparse(url).path or "/"
    return ARCHIVE_PATH_PREFIX.sub("", path) or "/"


def _page_weight(url: str, title: str, is_home: bool) -> tuple[float, bool]:
    """(weight, editorial) for evidence found on a page."""
    path = _page_path(url)
    if EDITORIAL_PAGE.search(path) or (not is_home and ARTICLE_TITLE.search(title or "")):
        return 0.25, True
    if is_home or path.strip("/") == "":
        return 1.0, False
    if SERVICE_PAGE.search(path) or SERVICE_PAGE.search(title or ""):
        return 1.0, False
    if INFO_PAGE.search(path):
        return 0.8, False
    return 0.7, False


def _snippet(text: str, start: int, end: int, radius: int = 70) -> str:
    s = max(0, start - radius)
    e = min(len(text), end + radius)
    return " ".join(text[s:e].replace("|", " ").split())


def _label_key(label: str) -> str:
    words = [w for w in _norm(label).split() if w not in LABEL_GENERIC]
    return " ".join(words)


def _clean_label(segment: str, span: tuple[int, int], coordinated: bool = False) -> str:
    """The service name as written on the page: the matched words plus modifiers written right before them.

    "Residential Carpet Cleaning Services in Anchorage" -> "Residential Carpet Cleaning"; a coordinated list
    "Roof, Gutter & Window Cleaning" gives this service its own name ("Roof Cleaning"). Brand and city
    words around the service are left out.
    """
    match = " ".join(segment[span[0] : span[1]].split())
    if coordinated:
        first = LIST_SEPARATOR.split(match, maxsplit=1)[0].strip()
        tail = LABEL_TAIL.search(match)
        label = f"{first} {tail.group(1)}" if tail else match
    else:
        label = match
        before = segment[: span[0]].split()
        mods: list[str] = []
        while before and len(mods) < 2 and before[-1].strip(",:;").lower() in LABEL_MODIFIERS:
            mods.insert(0, before.pop().strip(",:;"))
        if mods:
            label = f"{' '.join(mods)} {label}"
    label = label.strip(" \t•·*-–—>»✓✔→:;,.")
    if label == label.lower() or label == label.upper():
        label = " ".join(w if w in ("and", "or", "of", "&", "/") else w[:1].upper() + w[1:].lower() for w in label.split())
    return label


def _segments(page_title: str, meta: str, text: str) -> list[str]:
    parts = [page_title or "", meta or ""] + (text or "").split("|")
    return [" ".join(p.split()) for p in parts if p and p.strip()]


# ===================================================================================== site profiling
def profile_site(crawl: "CrawlResult") -> SiteProfile:
    """Profile a crawled website: cleaning services offered (graded in context) and non-target signatures."""
    if not crawl.is_accessible:
        return SiteProfile(accessible=False)

    profile = SiteProfile(accessible=True)
    pages = crawl.all_pages
    profile.pages = len(pages)
    home = crawl.homepage
    profile.home_head = " ".join(filter(None, [home.title if home else "", home.meta_description if home else ""]))

    page_segments = [(_segments(p.title, p.meta_description, p.text_content), p) for p in pages]

    # Template segments (navigation / footer repeated on most pages) are discounted a little
    seen_on: dict[str, int] = {}
    for segs, _ in page_segments:
        for s in {s.lower() for s in segs if len(s) <= 80}:
            seen_on[s] = seen_on.get(s, 0) + 1
    template_min = max(3, int(0.6 * len(pages) + 0.999)) if len(pages) >= 3 else 10**6

    all_text_parts: list[str] = []
    for segs, page in page_segments:
        is_home = page is home
        weight, editorial = _page_weight(page.url, page.title, is_home)
        path_title = f"{_page_path(page.url)} {page.title or ''}"
        page_text = " | ".join(segs)
        all_text_parts.append(page_text)
        profile.word_count += sum(1 for w in page_text.split() if w != "|")
        profile.cleaning_mentions += len(CLEANING_VOCAB.findall(page_text))
        profile.residential_cues += sum(1 for s in segs if s.lower() != "home" and RESIDENTIAL_CUE.search(s))
        if not profile.commercial_only_statement:
            m = COMMERCIAL_ONLY_STATEMENT.search(page_text)
            if m:
                profile.commercial_only_statement = _snippet(page_text, m.start(), m.end())
        # A page dedicated to a service: its URL path or title names it (never the homepage or a blog post)
        dedicated_keys: set[str] = set()
        if not editorial and not is_home:
            dedicated_keys = {s.key for s in SERVICES if s.page_rx.search(path_title)}

        # A (non-home) page whose own content talks only to businesses: its services are commercial offers
        body_segs = [s for s in segs if seen_on.get(s.lower(), 0) < template_min]
        page_commercial = (
            not is_home
            and sum(len(COMMERCIAL_PAGE_CUE.findall(s)) for s in body_segs) >= 2
            and not any(RESIDENTIAL_CUE.search(s) for s in body_segs if s.lower() != "home")
        )

        addon_until = -1
        for i, seg in enumerate(segs):
            words = len(seg.split())
            if words <= 5 and ADDON_HEADING.search(seg):
                addon_until = i + 15
            if not _SERVICE_STEMS.search(seg):
                continue
            lower = seg.lower()
            # "Need House Cleaning? Try House Cleaning by Carole!": another business recommended, not a service
            if REFERRAL_CONTEXT.search(seg):
                continue
            template = seen_on.get(lower, 0) >= template_min
            checklist = len({m.group(0).lower() for m in CHECKLIST_WORDS.finditer(seg)}) >= 3
            addon = i <= addon_until and not ADDON_HEADING.search(seg)
            question = seg.rstrip().endswith("?")
            for svc in SERVICES:
                matches = list(svc.patterns.finditer(seg))
                descriptive = coordinated = generic = False
                if not matches and svc.coordinated is not None:
                    matches = list(svc.coordinated.finditer(seg))
                    coordinated = bool(matches)
                if not matches and svc.generic is not None:
                    matches = list(svc.generic.finditer(seg))
                    generic = bool(matches)
                if not matches and svc.descriptive is not None:
                    matches = list(svc.descriptive.finditer(seg))
                    descriptive = generic = bool(matches)  # "we clean your home like our own" is not a service
                if not matches and svc.listing is not None and words <= 3:
                    bare = seg.strip(" \t•·*-–—>»✓✔→:;,.").strip()
                    lm = svc.listing.search(bare)
                    if lm:
                        start = seg.find(bare)
                        matches = [_Span(start, start + len(bare))]
                if not matches:
                    continue
                m = matches[0]
                start, end = m.start(), m.end()
                before = seg[max(0, start - 60) : start]
                if NEGATION.search(before):
                    continue
                if svc.exclude_before is not None and svc.exclude_before.search(before):
                    continue
                window = seg[max(0, start - 40) : end + 40]
                if svc.exclude is not None and svc.exclude.search(window):
                    continue
                if descriptive and svc.key == "house" and HOUSE_DESCRIPTIVE_EXCLUDE.search(window):
                    continue
                if checklist and svc.key in TASK_SERVICES:
                    continue

                if svc.key in dedicated_keys and not template:
                    kind, base = "page", 3.0
                elif words <= 8 and not question and not SENTENCE_WORDS.search(seg):
                    kind, base = "listing", 2.0  # a heading / list item / menu entry
                elif OFFER_CONTEXT.search(seg):
                    kind, base = "statement", 2.0
                else:
                    kind, base = "mention", 1.0
                mult = weight if kind != "page" else 1.0
                if DIY_CONTEXT.search(seg) and kind != "page":
                    mult *= 0.4
                if RETAIL_CONTEXT.search(seg[max(0, start - 50) : end + 50]):
                    mult *= 0.3
                if CONTRAST_CONTEXT.search(seg[max(0, start - 60) : end + 60]):
                    mult *= 0.3  # "high-pressure washing can damage shingles", "instead of pressure washing"
                if template and kind != "page":
                    mult *= 0.85
                score = round(base * mult, 3)

                commercial = page_commercial or (
                    bool(COMMERCIAL_PREFIX.search(seg[max(0, start - 40) : start]))
                    and not RESIDENTIAL_PREFIX.search(seg[max(0, start - 40) : start])
                )
                label = svc.name if descriptive else _clean_label(seg, (start, end), coordinated)
                ev = profile.services.setdefault(svc.key, ServiceEvidence(svc.key))
                ev.hits.append(
                    ServiceHit(svc.key, label, page.url, kind, score, _snippet(seg, start, end), commercial, addon, generic, template)
                )

    # Generic maid phrases ("deep cleaning", "regular cleaning", "residential cleaning", "clean your home") only
    # count on a maid-service site: its homepage says so, or it names house cleaning specifically and reads like
    # one (kitchens, bathrooms, dusting, ...). On a carpet / duct / exterior cleaner's site they describe other work.
    text_all = " ".join(all_text_parts)
    profile.maid_words = len({m.group(0).lower() for m in MAID_VOCAB.finditer(text_all)})
    house = profile.services.get("house")
    specific_house = _aggregate([h for h in house.hits if not h.generic]) if house else 0.0
    maid_site = bool(MAID_HEAD.search(profile.home_head)) or (specific_house >= OFFERED_SCORE and profile.maid_words >= 5)

    # Per-service score: best evidence + a little for corroboration on other pages. Commercial-only mentions
    # and add-on extras still list the service, but cannot make it the residential main service.
    for ev in profile.services.values():
        hits = [h for h in ev.hits if maid_site or not h.generic]
        if ev.definition.strict:
            # Restoration-class services need their own evidence in page content, not just a menu label
            body = [h for h in hits if not h.template]
            if not body or max(h.score for h in body) < 1.8:
                hits = []
        ev.score = _aggregate(hits)
        ev.residential_score = _aggregate([h for h in hits if not h.commercial and not h.addon])
    profile.services = {k: v for k, v in profile.services.items() if v.score > 0}

    text = " | ".join(all_text_parts)
    profile.directory_score = float(len({m.group(0).lower() for m in DIRECTORY_PATTERNS.finditer(text)}))
    profile.software_score = float(len({m.group(0).lower() for m in SOFTWARE_PATTERNS.finditer(text)}))
    profile.commerce_score = float(len({m.group(0).lower() for m in COMMERCE_PATTERNS.finditer(text)}))
    profile.detailing_score = float(len(DETAILING_PATTERNS.findall(text)))
    profile.dry_cleaner_score = float(len(DRY_CLEANER_PATTERNS.findall(text)))
    best, best_n = "", 0
    for label, pattern in UNRELATED_INDUSTRIES:
        n = len(pattern.findall(text))
        if n > best_n:
            best, best_n = label, n
    profile.industry = best if best_n >= 2 else ""

    _decide_site_type(profile, crawl.domain)
    return profile


@dataclass
class _Span:
    _start: int
    _end: int

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end


def _aggregate(hits: list[ServiceHit]) -> float:
    if not hits:
        return 0.0
    best = max(h.score for h in hits)
    # Corroboration: other pages (or other blocks) that also present the service. A navigation / footer entry
    # repeated on every page is one piece of evidence, not one per page.
    body = [h for h in hits if not h.template]
    pages = len({h.url for h in body if h.score >= 0.5}) + (1 if len(body) < len(hits) else 0)
    others = sorted((h.score for h in body), reverse=True)[1:]
    bonus = min(0.75, 0.25 * max(0, pages - 1)) + min(0.5, 0.1 * len([s for s in others if s >= 0.5]))
    return round(best + bonus, 3)


def _decide_site_type(profile: SiteProfile, domain: str) -> None:
    head = profile.home_head
    # "We serve commercial clients only": service mentions are not residential offers
    residential = [] if profile.commercial_only_statement else profile.residential_ranked
    offered = profile.offered
    strong_service = any(e.score >= STRONG_SCORE for e in offered)

    # Businesses that are not cleaning contractors, whatever cleaning words they use
    if profile.software_score >= 4 or (profile.software_score >= 2 and SOFTWARE_HEAD.search(head)):
        _set(profile, T_SOFTWARE, NO, 0.85, "software / booking platform for cleaning businesses")
        return
    if profile.directory_score >= 3 or (profile.directory_score >= 2 and not strong_service):
        _set(profile, T_DIRECTORY, NO, 0.85, "directory / listing site that lists other companies")
        return
    residential_strength = sum(e.residential_score for e in residential)
    head_without_products = re.sub(r"cleaning\s+(?:supplies|products|equipment)", "", head, flags=re.I)
    if (profile.commerce_score >= 4 and residential_strength < 4.0) or (
        profile.commerce_score >= 2 and PRODUCTS_HEAD.search(head) and not CLEANING_HEAD.search(head_without_products)
    ):
        _set(profile, T_PRODUCTS, NO, 0.85, "sells cleaning products / equipment rather than cleaning services")
        return
    if PRODUCTS_HEAD.search(head) and not offered and re.search(r"\bproducts?\b|\bsupplies\b|\bequipment\b", head, re.I):
        # "Naturally Powerful, Plant-Based Household Products": a product brand with no cleaning services
        _set(profile, T_PRODUCTS, NO, 0.8, "cleaning-product brand (no cleaning services offered)")
        return
    if profile.detailing_score >= 3 and not residential and (
        DETAILING_HEAD.search(head) or profile.detailing_score * 2 >= profile.cleaning_mentions
    ):
        _set(profile, T_DETAILING, NO, 0.85, "auto detailing / car wash business")
        return
    if profile.dry_cleaner_score >= 3 and not residential and DRY_CLEANER_HEAD.search(head):
        _set(profile, T_DRY_CLEANER, NO, 0.85, "garment dry cleaner / laundry")
        return

    cleaning_site = profile.cleaning_dense or profile.head_is_cleaning or strong_service

    # A commercial janitorial company that also lists "construction cleaning" or "power washing" for its business
    # clients: heavy commercial content and (almost) no residential wording anywhere on the site
    commercial_strength = sum(e.score for e in profile.commercial_offered)
    if residential and commercial_strength >= 4.0 and not MAID_HEAD.search(head):
        commercial_head = bool(COMMERCIAL_HEAD.search(head)) and not RESIDENTIAL_HEAD.search(head)
        residential_strength = sum(e.residential_score for e in residential)
        if profile.residential_cues <= 2 or (commercial_head and commercial_strength >= residential_strength):
            residential = []

    if residential and cleaning_site:
        main = residential[0]
        _set(profile, main.definition.group, YES, _yes_strength(profile, residential), f"cleaning company offering {main.definition.name}")
        profile.main_service = main.key
        if len(residential) == 1 and not profile.cleaning_dense and not profile.head_is_cleaning:
            profile.site_type = T_MULTI
        return

    if offered and cleaning_site:
        commercial = profile.commercial_offered
        if commercial and profile.industry and not profile.head_is_cleaning and not profile.commercial_only_statement:
            # A snow-removal / paving / landscaping business whose only cleaning work is commercial sweeping
            names = ", ".join(e.label() for e in commercial[:3])
            _set(profile, T_UNRELATED, NO, 0.8, f"{profile.industry} (its only cleaning work is {names} for commercial clients)")
            return
        if commercial or profile.commercial_only_statement:
            names = ", ".join(e.label() for e in (commercial or offered)[:4])
            note = "states it serves commercial clients only" if profile.commercial_only_statement else f"lists only {names}"
            strength = 0.85 if profile.commercial_only_statement or profile.residential_cues == 0 else 0.75
            _set(profile, T_COMMERCIAL, NO, strength, f"commercial-only cleaning company ({note}; no residential services)")
            return
        # Only commercial-flavoured mentions of ranked services ("commercial window cleaning") and no residential wording
        ranked = [e for e in offered if e.definition.rank is not None]
        if ranked and profile.residential_cues == 0:
            names = ", ".join(e.label() for e in ranked[:4])
            _set(profile, T_COMMERCIAL, NO, 0.7, f"commercial-only cleaning company ({names} for businesses; no residential wording)")
            return
        _set(profile, T_UNKNOWN, "", 0.0, "cleaning website whose residential services are unclear")
        return

    if residential and not cleaning_site:
        # A non-cleaning business that lists a cleaning service among other trades (handyman, roofer, ...)
        main = residential[0]
        _set(profile, T_MULTI, YES, 0.62, f"{profile.industry or 'multi-trade business'} that also offers {main.definition.name}")
        profile.main_service = main.key
        return

    if profile.cleaning_dense or profile.head_is_cleaning:
        _set(profile, T_UNKNOWN, "", 0.0, "cleaning website without a clear service list")
        return

    if profile.industry:
        _set(profile, T_UNRELATED, NO, 0.8, profile.industry)
        return
    _set(profile, T_UNRELATED, "", 0.0, "website with no cleaning content")


def _yes_strength(profile: SiteProfile, residential: list[ServiceEvidence]) -> float:
    main = residential[0]
    strength = 0.72
    if main.score >= 2.5 or len(residential) >= 2:
        strength += 0.1
    if profile.cleaning_dense:
        strength += 0.05
    if profile.residential_cues >= 2:
        strength += 0.05
    if any(h.kind == "page" for h in main.hits):
        strength += 0.03
    return min(0.95, strength)


def _set(profile: SiteProfile, site_type: str, verdict: str, strength: float, summary: str) -> None:
    profile.site_type = site_type
    profile.verdict = verdict
    profile.strength = strength
    profile.summary = summary


# ======================================================================================= name analysis
FRANCHISES: tuple[tuple[str, str], ...] = (
    (r"\bmerry maids\b|\bmolly maid\b|\bmaid ?pro\b|\bthe maids\b|\btwo maids\b|\bmaid brigade\b|\bmaid right\b|\bhome cleaning centers\b|\bmaids com\b|\bmaid in\b|\bmaidly\b|\bsparkly maid\b", "house"),
    (r"\bchem ?dry\b|\bstanley steemer\b|\bzerorez\b|\boxi ?fresh\b|\bheavens? best\b|\bcoit\b|\bsears carpet\b|\bsteamatic\b", "carpet"),
    (r"\bservpro\b|\bservicemaster restore\b|\bpaul davis\b|\bpuroclean\b|\brainbow (?:international|restoration)\b|\b911 restoration\b|\bbelfor\b|\brestoration 1\b|\bfirst restoration\b", "water"),
    (r"\bfish window\b|\bwindow genie\b|\bmen in kilts\b|\bsqueegee squad\b|\bshack shine\b|\bwindow hero\b", "window"),
    (r"\bdryer vent wizard\b|\bdryer vent squad\b", "dryer_vent"),
    (r"\bbar b clean\b|\bbbq ?clean\b", "grill"),
    (r"\bjan ?pro\b|\bcoverall\b|\bjani ?king\b|\boffice pride\b|\bstratus (?:building|clean)\b|\bcity ?wide\b|\banago\b|\bvanguard clean\b|\bservicemaster clean\b|\bopenworks\b|\bbms building\b|\bharvard maintenance\b|\babm\b", "commercial"),
)
NAME_SERVICE_KEYWORDS: tuple[tuple[str, str, float], ...] = (
    (r"\bhoard", "hoarding", 0.8),
    (r"\bmou?ld\b", "mold", 0.8),
    (r"\bseptic\b", "septic", 0.8),
    (r"\bbio ?hazard\b|\bcrime scene\b|\btrauma\b", "biohazard", 0.85),
    (r"\bfire\b.*\b(?:restoration|damage)\b|\bwater damage\b|\bflood\b|\brestoration\b", "water", 0.6),
    (r"\bpost construction\b|\bconstruction clean", "post_construction", 0.8),
    (r"\broof (?:clean|wash)|\bmoss\b", "roof", 0.85),
    (r"\bsoft ?wash|\bhouse wash", "house_wash", 0.85),
    (r"\bducts?\b|\bair vent\b", "air_duct", 0.85),
    (r"\bpressure ?wash|\bpower ?wash|\bpressure\b|\bexterior clean", "pressure", 0.85),
    (r"\bcarpets?\b|\brugs?\b|\bsteam(?:er|ers|way|ing)?\b|\bdry carpet\b", "carpet", 0.8),
    (r"\bgrout\b|\btile\b", "tile", 0.7),
    (r"\bgutters?\b", "gutter", 0.65),
    (r"\bchimney\b|\bchim chim\b", "chimney", 0.8),
    (r"\bwindows? (?:clean|wash)|\bwindow washers?\b|\bsqueegee\b", "window", 0.85),
    (r"\bsolar\b", "solar", 0.7),
    (r"\bupholstery\b", "upholstery", 0.8),
    (r"\bmove (?:in|out)\b", "move", 0.8),
    (r"\bdryer vents?\b", "dryer_vent", 0.85),
    (r"\bmaids?\b|\bhouse ?clean|\bhome ?clean|\bhousekeep|\bresidential clean|\bcleaning lady\b", "house", 0.85),
    (r"\bblinds?\b", "blinds", 0.6),
    (r"\bpools?\b", "pool", 0.6),
    (r"\b(?:trash|garbage|bin|can) (?:clean|wash)|\bcan man\b|\bbin\b", "trash_can", 0.55),
    (r"\bairbnb\b|\bvacation rental\b", "vacation", 0.8),
    (r"\bjanitorial\b|\bcustodial\b|\bcommercial clean|\boffice clean|\bbuilding (?:maintenance|services)\b|\bfacility\b|\bjanitor\b", "commercial", 0.8),
)
NAME_GENERIC_CLEANING = re.compile(r"\bclean\w*\b|\bsparkl\w*\b|\bspotless\b|\bshine\b|\btidy\b|\bspic\b|\bsqueaky\b|\bwash\w*\b|\bscrub\w*\b|\bdust\w*\b|\bsaniti\w+\b")
NAME_NOT_CLEANING: tuple[tuple[str, str], ...] = (
    (r"\bsnow (?:removal|plow\w*)\b", "snow removal company"),
    (r"\blandscap\w*\b|\blawn\b|\bmowing\b|\btree service\b", "landscaping company"),
    (r"\broofing\b|\broofers?\b", "roofing contractor"),
    (r"\bpainting\b|\bpainters?\b", "painting contractor"),
    (r"\bplumbing\b|\bplumbers?\b|\belectric(?:al)?\b|\bhvac\b|\bheating\b", "plumbing / HVAC / electrical contractor"),
    (r"\bwindows? and doors?\b|\bdoors? and windows?\b|\bwindow (?:install\w*|replacement|world|depot|tint\w*)\b|\bglass\b", "window / glass installer"),
    (r"\bflooring\b|\bconstruction\b|\bremodel\w*\b|\bbuilders?\b|\bhandyman\b", "construction / handyman business"),
    (r"\bpest\b|\bexterminat\w*\b", "pest control company"),
    (r"\bmoving\b|\bmovers\b|\bjunk\b|\bhauling\b", "moving / junk removal company"),
    (r"\bproperty management\b|\brealty\b|\breal estate\b", "property management / real estate business"),
    (r"\bcar wash\b|\bdetail(?:ing|ers?)?\b|\bauto spa\b", "auto detailing / car wash"),
    (r"\bdry cleaners?\b|\blaundromat\b|\blaundry\b|\btailor\w*\b", "garment dry cleaner / laundry"),
    (r"\bsupply\b|\bsupplies\b|\bequipment\b|\bdistribut\w*\b", "cleaning products / supply business"),
)
NAME_TRADES_OVER_CLEANING = frozenset({"garment dry cleaner / laundry", "cleaning products / supply business", "auto detailing / car wash"})
CATEGORY_HINTS: dict[str, str] = {
    "home cleaning": "house", "carpet cleaning": "carpet", "janitorial service": "commercial", "office cleaning": "commercial",
    "b2b cleaning and waste management": "commercial", "industrial cleaning service": "commercial", "cleaning service": "generic",
    "pressure washing": "pressure", "window washing": "window", "air duct cleaning service": "air_duct", "gutter service": "gutter",
    "damage restoration": "water", "fire and water damage restoration": "water", "grout service": "tile", "solar panel cleaning": "solar",
}
SEGMENT_HINTS: dict[str, str] = {
    "residential / maid": "house", "carpet / upholstery / floor": "carpet", "pressure washing / exterior": "pressure",
    "commercial / janitorial": "commercial", "general cleaning": "generic", "window cleaning": "window", "air duct / vent": "air_duct",
    "solar panel cleaning": "solar",
}
NON_CLEANING_CATEGORIES = frozenset({
    "windows installation", "painting", "landscaping", "roofing", "hvac service", "flooring contractor", "property management",
    "handyman", "building or construction service",
})


@dataclass
class NameSignal:
    """What the business name (plus the list's category / segment) says."""

    verdict: str = ""  # Yes | No | ""
    strength: float = 0.0
    service: str = ""  # service key hinted by the name ("commercial" / "generic" possible)
    terms: list[str] = field(default_factory=list)
    brand: str = ""
    source: str = ""  # "business name" | "category" | "segment"
    trade: str = ""  # non-cleaning trade named

    @property
    def commercial(self) -> bool:
        return self.service == "commercial"

    @property
    def hint_name(self) -> str:
        if self.service in SERVICE_BY_KEY:
            return SERVICE_BY_KEY[self.service].name
        return "cleaning" if self.service == "generic" else ""


def analyze_name(business_name: str, category: str = "", segment: str = "") -> NameSignal:
    """Name verdict (category / segment only fill in when the name gives no signal)."""
    text = _norm(clean_company_name(business_name) or business_name)
    if text:
        for pattern, key in FRANCHISES:
            m = re.search(pattern, text)
            if m:
                return NameSignal(YES, 0.9, key, [m.group(0)], m.group(0), "business name")
        hits = [(key, w, m.group(0)) for pattern, key, w in NAME_SERVICE_KEYWORDS if (m := re.search(pattern, text))]
        generic = NAME_GENERIC_CLEANING.search(text)
        trade = next(((label, m.group(0)) for pattern, label in NAME_NOT_CLEANING if (m := re.search(pattern, text))), None)
        # "ABC Dry Cleaners", "Pro Cleaning Supply": cleaning words, but not a cleaning service business
        if trade and trade[0] in NAME_TRADES_OVER_CLEANING and not hits:
            return NameSignal(NO, 0.7, "", [trade[1]], source="business name", trade=trade[0])
        if hits:
            # The most specific residential hint wins; commercial only when nothing residential is named
            residential = [h for h in hits if h[0] != "commercial"]
            key, weight, term = max(residential or hits, key=lambda h: h[1])
            if trade and not generic and weight < 0.8:
                return NameSignal(NO, 0.6, "", [trade[1]], source="business name", trade=trade[0])
            return NameSignal(YES, min(0.9, weight + (0.05 if generic else 0.0)), key, [h[2] for h in hits], source="business name")
        if trade and not generic:
            return NameSignal(NO, 0.7, "", [trade[1]], source="business name", trade=trade[0])
        if generic:
            return NameSignal(YES, 0.7 if "clean" in generic.group(0) else 0.45, "generic", [generic.group(0)], source="business name")

    cat = (category or "").strip().lower()
    if cat in CATEGORY_HINTS:
        return NameSignal(YES, 0.4, CATEGORY_HINTS[cat], [category], source="category")
    if cat in NON_CLEANING_CATEGORIES:
        return NameSignal(NO, 0.35, "", [category], source="category", trade=category)
    seg = (segment or "").strip().lower()
    if seg in SEGMENT_HINTS:
        return NameSignal(YES, 0.3, SEGMENT_HINTS[seg], [segment], source="segment")
    return NameSignal()


def non_site_link(url: str) -> str:
    """The host when a website cell holds a map share / booking / chat link instead of a website, else ''."""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = f"https://{u}"
    host = urlparse(u).netloc.lower().removeprefix("www.")
    if host in NON_SITE_HOSTS:
        return host
    return ""


def directory_link(url: str) -> str:
    """The host when the website is a directory / social listing page rather than the business's own site."""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = f"https://{u}"
    host = urlparse(u).netloc.lower().removeprefix("www.")
    return host if host in DIRECTORY_DOMAINS else ""


# ========================================================================================= classifier
MIN_WORDS_FOR_INDUSTRY = 150


@dataclass
class _Decision:
    verdict: str
    confidence: float
    business_type: str
    basis: str
    reason: str
    short: str
    use_site_services: bool = False


class CleaningServiceClassifier:
    """Step 1 qualifies the business, step 2 extracts its services, step 3 picks the main service."""

    def classify(
        self,
        crawl: "CrawlResult | None",
        lead: LeadInfo,
        profile: SiteProfile | None = None,
        archived_snapshot: str = "",
        live_status: str = "",
        website_note: str = "",
    ) -> CleaningClassificationResult:
        name = analyze_name(lead.business_name, lead.category, lead.segment)

        if crawl is None:
            profile = SiteProfile(accessible=False)
            website_status = "Missing URL"
        else:
            crawl = downgrade_unusable(crawl)
            if profile is None or profile.accessible != crawl.is_accessible:
                profile = profile_site(crawl)
            website_status = crawl.status_category or ("OK" if crawl.is_accessible else "Connection Error")

        d = self._fuse(name, profile, lead, crawl, website_note)
        conf, reason, short = d.confidence, d.reason, d.short

        if archived_snapshot and profile.accessible:
            year = int(archived_snapshot[:4]) if archived_snapshot[:4].isdigit() else 0
            age = datetime.now().year - year if year else 99
            snap = f"{archived_snapshot[:4]}-{archived_snapshot[4:6]}-{archived_snapshot[6:8]}"
            website_status = f"Archived Copy (live: {live_status})"
            reason = f"Live site unavailable ({live_status}); evaluated Wayback Machine snapshot from {snap}. {reason}"
            if d.basis.startswith("Website"):
                conf *= 0.9
                short = f"{short} (archived {snap[:4]} copy)"
                if age > 4:
                    conf = min(conf, 0.5)
                    reason += " Snapshot is over 4 years old; current business needs verification."

        conf = round(max(0.05, min(0.99, conf)), 2)
        if conf >= REVIEW_THRESHOLD:
            bucket = d.verdict
        else:
            bucket = "Uncertain" if profile.accessible else "Unreachable"

        # Step 2 + 3 only for cleaning companies, and only from what the website says
        main_service = ""
        services: list[str] = []
        phrases: list[str] = []
        if d.verdict == YES and d.use_site_services and profile.accessible:
            services = profile.service_labels()
            if profile.main_service:
                main_service = SERVICE_BY_KEY[profile.main_service].name
                main_ev = profile.services[profile.main_service]
                phrases = [h.snippet[:140] for h in sorted(main_ev.hits, key=lambda h: -h.score)[:2]]
            elif profile.residential_ranked:
                main_ev = profile.residential_ranked[0]
                main_service = main_ev.definition.name
                phrases = [h.snippet[:140] for h in sorted(main_ev.hits, key=lambda h: -h.score)[:2]]

        return CleaningClassificationResult(
            classification=bucket,
            decision_label=d.verdict,
            confidence=conf,
            website_status=website_status,
            main_service=main_service,
            services=services,
            reasoning=reason,
            short_reason=f"{d.verdict}: {short}",
            business_type=d.business_type,
            decision_basis=d.basis,
            evidence_urls=profile.evidence_urls() if d.basis.startswith("Website") else [],
            evidence_phrases=phrases,
            access_error=(crawl.error_message or "") if crawl is not None and not crawl.is_accessible else "",
            pages_evaluated=profile.pages,
            crawl_details=_crawl_details(crawl) if crawl is not None else "",
            site_accessible=profile.accessible,
        )

    # ------------------------------------------------------------------ fusion
    def _fuse(self, name: NameSignal, site: SiteProfile, lead: LeadInfo, crawl: "CrawlResult | None", website_note: str) -> _Decision:
        domain = crawl.domain if crawl is not None else ""
        name_desc = _name_desc(name)

        # 1. The website gives its own verdict
        if site.accessible and site.verdict == YES:
            return self._site_yes(name, site, name_desc)
        if site.accessible and site.verdict == NO:
            return self._site_no(name, site, name_desc, domain)

        # 2. A cleaning website whose residential services could not be pinned down
        if site.accessible and (site.cleaning_dense or site.head_is_cleaning) and site.site_type == T_UNKNOWN:
            listed = ", ".join(e.definition.name for e in site.offered[:4])
            what = f"Website {domain} is about cleaning but " + (
                f"its listed services ({listed}) are not clearly residential." if listed else "lists no specific cleaning services."
            )
            conf = 0.5 if name.verdict == YES and name.source == "business name" else 0.42
            return _Decision(
                YES, conf, T_UNKNOWN, "Website (services unclear)",
                f"{what} Treated as a cleaning company ({name_desc}); main service could not be confirmed. Review manually.",
                "cleaning website without a clear residential service list",
                use_site_services=True,
            )

        # 3. A substantial website with no cleaning content
        if site.accessible and site.site_type == T_UNRELATED and site.word_count >= MIN_WORDS_FOR_INDUSTRY:
            note = f"Website {domain} has no cleaning services ({site.summary})."
            if name.verdict == YES and name.source == "business name" and name.strength >= 0.7:
                return _Decision(
                    YES, 0.45, T_UNKNOWN, "Name (website conflicts)",
                    f"{note} But the name ({name_desc}) indicates a cleaning business; the website may not be its own. Review manually.",
                    f"name says cleaning; website {domain} shows no cleaning",
                )
            if name.verdict == NO:
                return _Decision(NO, 0.8, T_UNRELATED, "Website + Name", f"{note} Name agrees ({name_desc}).", "no cleaning on website; name agrees")
            return _Decision(
                NO, 0.55, T_UNRELATED, "Website (no cleaning content)",
                f"{note} The business could not be identified as a cleaning company. Review manually.",
                f"website {domain} has no cleaning content",
            )

        # 4. No usable website: the name decides, with low confidence
        why, why_short = _why_site_unusable(site, domain, crawl, lead, website_note)
        if name.verdict == YES:
            if name.brand:
                conf, basis = 0.55, "Name Only (known franchise)"
            elif name.source == "business name":
                conf, basis = min(0.5, 0.15 + 0.45 * name.strength), "Name Only"
            else:
                conf, basis = 0.3, f"List {name.source.title()} Only"
            hint = f" Name suggests {name.hint_name}." if name.hint_name and name.hint_name != "cleaning" else ""
            commercial = " The name suggests commercial / janitorial work; residential services are unverified." if name.commercial else ""
            return _Decision(
                YES, conf, T_UNKNOWN, basis,
                f"{why} Classified from the {name.source}: {name_desc}.{hint}{commercial} Services could not be read from a website; review manually.",
                f"{name.source} {name_desc} ({why_short})",
            )
        if name.verdict == NO:
            return _Decision(
                NO, 0.45 if name.source == "business name" else 0.35, T_UNRELATED, "Name Only",
                f"{why} The {name.source} indicates {name.trade or 'a non-cleaning business'} ({name_desc}). Review manually.",
                f"{name.source} suggests {name.trade or 'non-cleaning'} ({why_short})",
            )
        return _Decision(
            YES, 0.25, T_UNKNOWN, "No Evidence",
            f"{why} The name gives no trade signal; defaulted to Yes because the record comes from a cleaning-business list. Review manually.",
            f"no evidence either way ({why_short})",
        )

    def _site_yes(self, name: NameSignal, site: SiteProfile, name_desc: str) -> _Decision:
        main = SERVICE_BY_KEY[site.main_service]
        ranked = site.residential_ranked
        others = [e.definition.name for e in ranked[1:6]]
        commercial = [e.definition.name for e in site.commercial_offered]
        best = site.services[site.main_service].best
        how = {"page": "has a dedicated page", "listing": "is listed as a service", "statement": "is offered in the site text", "mention": "is mentioned"}[best.kind]
        desc = (
            f"Website is a cleaning company: {main.name} {how} ({best.url})"
            + (f"; also offers {', '.join(others)}" if others else "")
            + (f"; commercial: {', '.join(commercial)}" if commercial else "")
            + f". Main service = {main.name} (highest-value residential service offered)."
        )
        conf = site.strength
        if site.site_type == T_MULTI:
            desc += " Cleaning is one service among other trades."
        if name.verdict == YES:
            conf = min(0.97, conf + 0.04 + 0.04 * name.strength)
            return _Decision(YES, conf, site.site_type, "Website + Name", f"{desc} Name agrees ({name_desc}).", f"website offers {main.name}; name agrees", True)
        if name.verdict == NO and name.source == "business name":
            conf -= 0.1
            return _Decision(YES, conf, site.site_type, "Website (name disagrees)", f"{desc} The name ({name_desc}) suggests otherwise; the website was trusted.", f"website offers {main.name}; name disagrees", True)
        return _Decision(YES, conf, site.site_type, "Website", desc, f"website offers {main.name}", True)

    def _site_no(self, name: NameSignal, site: SiteProfile, name_desc: str, domain: str) -> _Decision:
        desc = f"Website {domain} is {_a(site.summary)}."
        conf = site.strength
        if site.site_type == T_COMMERCIAL:
            if name.verdict == YES and name.service in ("house", "move", "deep", "vacation") and name.source == "business name":
                return _Decision(
                    NO, 0.5, T_COMMERCIAL, "Website (name disagrees)",
                    f"{desc} But the name ({name_desc}) suggests residential cleaning; residential pages may not have been crawled. Review manually.",
                    "website lists commercial cleaning only; name suggests residential",
                )
            if name.commercial:
                return _Decision(
                    NO, min(0.95, conf + 0.1), T_COMMERCIAL, "Website + Name",
                    f"{desc} Not a residential cleaning contractor. Name agrees ({name_desc}).",
                    "commercial-only cleaning; name agrees",
                )
            return _Decision(NO, conf, T_COMMERCIAL, "Website", f"{desc} Not a residential cleaning contractor.", "commercial-only cleaning; no residential services")
        if site.site_type == T_UNRELATED and name.verdict == YES and name.source == "business name" and name.strength >= 0.7:
            return _Decision(
                YES, 0.45, T_UNKNOWN, "Name (website conflicts)",
                f"{desc} But the name ({name_desc}) indicates a cleaning business; the website may not be its own. Review manually.",
                f"name says cleaning; website is {_a(site.summary)}",
            )
        if name.verdict == YES and name.source == "business name" and name.strength >= 0.7:
            conf -= 0.1
            return _Decision(NO, conf, site.site_type, "Website (name disagrees)", f"{desc} The name ({name_desc}) suggests cleaning; the website was trusted.", f"website is {_a(site.summary)}; name disagrees")
        if name.verdict == NO:
            conf = min(0.95, conf + 0.05)
        return _Decision(NO, conf, site.site_type, "Website", desc, f"website is {_a(site.summary)}")


def _name_desc(name: NameSignal) -> str:
    if not name.verdict:
        return "no trade signal in name"
    if name.verdict == NO:
        return f"{name.source} '{', '.join(name.terms[:2])}' -> {name.trade or 'not cleaning'}"
    what = "known franchise" if name.brand else (name.hint_name or "cleaning")
    if name.commercial:
        what = "commercial / janitorial cleaning"
    return f"{name.source} '{', '.join(name.terms[:3])}' -> {what}"


def _a(phrase: str) -> str:
    return f"{'an' if phrase[:1].lower() in 'aeiou' else 'a'} {phrase}"


def _why_site_unusable(site: SiteProfile, domain: str, crawl: "CrawlResult | None", lead: LeadInfo, website_note: str) -> tuple[str, str]:
    if website_note:
        return website_note, "no business website"
    if crawl is None:
        if lead.website:
            return f"Website value '{lead.website[:80]}' is not a usable URL.", "invalid website"
        return "No website in the record.", "no website"
    if not site.accessible:
        status = crawl.status_category or "unreachable"
        messages = {
            "Parked Domain": f"Website {domain} is parked/for sale.",
            "DNS Failure": f"Website {domain} does not resolve (dead domain).",
            "Placeholder Page": f"Website {domain} is only a placeholder page (coming soon / default hosting page).",
            "Bot Challenge": f"Website {domain} is behind bot protection (not bypassed).",
            "Empty Page": f"Website {domain} has no readable content.",
        }
        return messages.get(status, f"Website {domain} could not be read ({status})."), f"website {status.lower()}"
    return f"Website {domain} has too little content to show its services ({site.word_count} words).", "website has too little content"


def _crawl_details(crawl: "CrawlResult") -> str:
    parts: list[str] = []
    pages = crawl.pages_fetched or len(crawl.all_pages)
    if crawl.is_accessible:
        parts.append(f"{pages} page(s) evaluated")
    if crawl.total_attempts:
        parts.append(f"{crawl.total_attempts} request attempt(s)")
    if crawl.sitemap_checked:
        parts.append("sitemap checked")
    if crawl.failed_urls:
        parts.append(f"{len(crawl.failed_urls)} page(s) failed")
    if crawl.robots_blocked_urls:
        parts.append(f"{len(crawl.robots_blocked_urls)} page(s) disallowed by robots.txt")
    if crawl.from_cache:
        parts.append("cached crawl")
    if crawl.incomplete_reason:
        parts.append(f"incomplete: {crawl.incomplete_reason}")
    parts.extend(crawl.notes or [])
    return "; ".join(parts)
