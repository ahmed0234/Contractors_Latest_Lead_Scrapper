"""Auto repair qualification: does a business PROVIDE vehicle repair / maintenance services to customers?

Wanted (Yes): general repair, mechanics, oil change / quick lube, tires, brakes, exhaust, transmission,
A/C, collision / body shops, auto glass, diesel / fleet repair, mobile mechanics, and similar car
services. Not wanted (No): dealerships and car sales lots (even with a service department, and their
dealer-owned collision centers), rentals / leasing, parking, trucking / transportation, towing-only,
wash / detailing, tint / wrap / audio, parts retail, non-road vehicles (boats, RVs, golf carts,
powersports, heavy equipment), and unrelated businesses.

The verdict for each lead fuses two explainable signals:

1. **Name** (`analyze_name`): Business_Name and dba_name against a knowledge base of repair chains
   (Midas, Firestone, Caliber Collision, ...), non-repair chains (AutoZone, CarMax, Enterprise, ...),
   OEM brands (dealership names), and repair / dealer / excluded-trade keywords. The dba (trading
   name) outranks the legal name, which is often a holding company.
2. **Website profile** (`profile_site`): computed once per crawled domain. Every page is scanned for
   repair-service evidence (brakes, A/C, diagnostics, collision, ...), dealership evidence
   (inventory, test drives, new & used vehicles, financing, OEM franchise), excluded trades (parts
   retail, towing-only, wash/detailing, tint/wrap/audio, rental, parking, marine, golf carts, RV,
   powersports, heavy equipment, transport) and unrelated industries. Evidence is weighted by page
   type (service pages > homepage > about/contact > blog) and negated mentions are ignored.

The website on the row is accepted as the business's own site: there is no ownership check (phone /
address / name matching). `AutoRepairClassifier.classify` lets an automotive site decide (name
agreement raises confidence, disagreement lowers it); a non-automotive site (an accountant's or parent
company's email domain) only loses to a clearly repair-trade name, and then with reduced confidence so
the row is flagged as borderline; without a readable site the name decides with low confidence (known
chains excepted). Every verdict carries a one-line `short_reason` for the review report.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from contractor_scraper.discovery.company_identity import domain_slug
from contractor_scraper.normalization.cleaners import clean_company_name

if TYPE_CHECKING:
    from contractor_scraper.discovery.crawler import CrawlResult

CLASSIFIER_VERSION = "2026.11-auto-repair-4"

YES = "Yes"
NO = "No"

# Confidence at or above which a verdict is final; below it the lead goes to manual review
REVIEW_THRESHOLD = 0.6
# Below this confidence (or when the signals conflict) a row is flagged "borderline" in the reason report
BORDERLINE_THRESHOLD = 0.7

# Business types
T_GENERAL = "General Auto Repair"
T_COLLISION = "Collision / Auto Body"
T_TIRE = "Tire & Auto Service"
T_LUBE = "Oil Change / Quick Lube"
T_TRANSMISSION = "Transmission Specialist"
T_EXHAUST = "Muffler / Exhaust / Brakes"
T_GLASS = "Auto Glass"
T_AC = "Auto A/C & Radiator"
T_FLEET = "Diesel / Truck / Fleet Repair"
T_MOBILE = "Mobile Mechanic"
T_SPECIALTY = "Specialty Repair / Restoration"
T_DEALER = "Car Dealership"
T_DEALER_OWNED = "Dealership-owned Collision/Service Center"
T_PARTS = "Auto Parts Retail"
T_TOWING = "Towing Only"
T_WASH = "Car Wash / Detailing"
T_TINT = "Tint / Wrap / Audio"
T_RENTAL = "Car Rental / Leasing"
T_PARKING = "Parking / Valet"
T_MARINE = "Marine / Boat"
T_GOLF = "Golf Cart"
T_RV = "RV Dealer"
T_POWERSPORTS = "Powersports Dealer"
T_EQUIPMENT = "Heavy Equipment"
T_TRANSPORT = "Transport / Logistics"
T_UNRELATED = "Unrelated Business"
T_UNKNOWN = "Unknown"

REPAIR_TYPES = frozenset({T_GENERAL, T_COLLISION, T_TIRE, T_LUBE, T_TRANSMISSION, T_EXHAUST, T_GLASS, T_AC, T_FLEET, T_MOBILE, T_SPECIALTY})


def _norm(text: str) -> str:
    """Lowercase, '&' -> 'and', punctuation -> spaces (keeps the '/' of 'a/c' and digits)."""
    text = (text or "").lower().replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9/]+", " ", text).split())


def _rx(*alternatives: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternatives), re.IGNORECASE)


# ======================================================================================= knowledge base
# (pattern on normalized name text, business type) — repair chains/franchises
REPAIR_CHAINS: tuple[tuple[str, str], ...] = (
    (r"\bmidas\b", T_EXHAUST),
    (r"\bmeineke\b", T_EXHAUST),
    (r"\bprecision tune\b", T_GENERAL),
    (r"\bpep ?boys\b", T_GENERAL),
    (r"\bfirestone\b", T_TIRE),
    (r"\btires plus\b", T_TIRE),
    (r"\bwheel works\b", T_TIRE),
    (r"\bhibdon\b", T_TIRE),
    (r"\bgoodyear\b", T_TIRE),
    (r"\bjiffy lube\b", T_LUBE),
    (r"\bvalvoline\b", T_LUBE),
    (r"\btake 5\b|\btake five oil\b", T_LUBE),
    (r"\bexpress oil\b", T_LUBE),
    (r"\bgrease monkey\b", T_LUBE),
    (r"\bmr lube\b|\bspeedee oil\b|\bkwik kar\b|\bsuper lube\b", T_LUBE),
    (r"\baamco\b|\bcottman\b|\bmr transmission\b|\blee myles\b", T_TRANSMISSION),
    (r"\bmaaco\b|\bcaliber collision\b|\bcrash champions\b|\bgerber collision\b|\bservice king\b", T_COLLISION),
    (r"\bcarstar\b|\bfix auto\b|\babra auto\b|\bjoe hudson\w*\b|\bclassic collision\b|\bdent wizard\b", T_COLLISION),
    (r"\bchristian brothers automotive\b|\bmonro\b|\btuffy\b|\bcar x\b|\bcarx\b", T_GENERAL),
    (r"\bntb\b|\bnational tire and battery\b|\btire kingdom\b|\bbig o tires?\b|\bdiscount tire\b", T_TIRE),
    (r"\bmavis\b|\bsun auto\b|\btire choice\b|\bles schwab\b|\bcallaghan tire\b|\bkauffman tire\b", T_TIRE),
    (r"\brnr tire\b|\brent n roll\b|\bbridgestone\b", T_TIRE),
    (r"\bbrakes plus\b|\bbrake masters?\b|\bbrake check\b", T_EXHAUST),
    (r"\bice cold air\b", T_AC),
    (r"\bsafelite\b|\bglass doctor\b", T_GLASS),
    (r"\bnapa auto ?care\b|\bsears auto\b|\baaa car care\b", T_GENERAL),
    (r"\bsnider fleet\b", T_FLEET),
)

# Non-repair chains / well-known businesses
NON_REPAIR_CHAINS: tuple[tuple[str, str], ...] = (
    (r"\bautozone\b|\badvance auto parts?\b|\bo ?reilly auto\b|\bnapa auto parts\b|\bcarquest\b|\bbatteries plus\b", T_PARTS),
    (r"\bcarmax\b|\bcarvana\b|\bautonation\b|\becho ?park\b|\blithia\b|\bhendrick\b|\bpenske automotive\b", T_DEALER),
    (r"\bsonic automotive\b|\basbury\b|\bgroup 1 auto\b|\bdrivetime\b|\bcar ?sense\b|\bmobilityworks\b", T_DEALER),
    (r"\brush truck\b|\brush enterprises\b", T_DEALER),
    (r"\benterprise rent\b|\bhertz\b|\bavis\b|\bbudget rent\b|\balamo\b|\bsixt\b|\bu ?haul\b|\bpenske truck\b|\bryder\b", T_RENTAL),
    (r"\btint world\b|\bziebart\b", T_TINT),
    (r"\bring power\b|\bcaterpillar\b|\bunited rentals\b", T_EQUIPMENT),
    (r"\bgeneral rv\b|\bcamping world\b|\blazydays\b", T_RV),
    (r"\bgreyhound\b", T_TRANSPORT),
)

OEM_BRANDS: tuple[str, ...] = (
    "toyota", "lexus", "scion", "honda", "acura", "nissan", "infiniti", "ford", "lincoln", "chevrolet", "chevy",
    "gmc", "buick", "cadillac", "chrysler", "dodge", "jeep", "ram", "cdjr", "hyundai", "genesis", "kia",
    "mazda", "subaru", "volkswagen", "vw", "audi", "porsche", "bmw", "mini", "mercedes", "mercedes benz",
    "volvo", "jaguar", "land rover", "alfa romeo", "fiat", "maserati", "mitsubishi", "tesla", "polestar",
    "bentley", "rolls royce", "ferrari", "lamborghini", "mclaren", "aston martin", "lotus", "harley davidson",
    "freightliner", "peterbilt", "kenworth", "mack", "isuzu", "hino", "western star",
)
_OEM_RE = re.compile(r"\b(" + "|".join(re.escape(b) for b in OEM_BRANDS) + r")\b")
# "ram" and "mini" are common words: only count them next to another dealer cue
_AMBIGUOUS_OEM = frozenset({"ram", "mini", "genesis", "lotus", "mack", "fiat"})

# Keyword signals in names: (pattern, business type, weight)
NAME_REPAIR_KEYWORDS: tuple[tuple[str, str, float], ...] = (
    (r"\bcollision\b|\bcrash\b|\bauto ?body\b|\bbody (?:shop|works|and paint|and glass|and frame|repair)\b|\bpaint and body\b|\bdent\b|\bpaintless\b", T_COLLISION, 0.8),
    (r"\btransmissions?\b", T_TRANSMISSION, 0.8),
    (r"\bmufflers?\b|\bexhaust\b|\bbrakes?\b", T_EXHAUST, 0.8),
    (r"\boil change\b|\blube\b|\blubes\b|\bquick lube\b|\btune ?up\b", T_LUBE, 0.8),
    (r"\bauto glass\b|\bwindshields?\b", T_GLASS, 0.8),
    (r"\bradiators?\b|\bauto a/?c\b|\bauto air\b|\bcold air\b", T_AC, 0.75),
    (r"\bmobile (?:mechanic|auto|repair|tire)\b", T_MOBILE, 0.8),
    (r"\broad service\b|\bon site (?:repair|service)\b", T_MOBILE, 0.7),
    (r"\bdiesel\b|\btruck (?:repair|service)\b|\bfleet\b|\btrailer repair\b", T_FLEET, 0.7),
    (r"\btires?\b|\btyres?\b|\balignment\b|\bwheel and tire\b", T_TIRE, 0.7),
    (
        r"\b(?:auto|automotive|car|vehicle|truck|engine|motor) ?(?:repairs?|service|services|care|clinic|doctor|tech|techs|shop|electric|electrical|mechanics?)\b"
        r"|\bmechanic(?:s|al)?\b|\bauto ?repair\b|\brepairs?\b|\bsales and service\b",
        T_GENERAL,
        0.75,
    ),
    (r"\bautomotive\b|\bgarage\b|\bmotor ?w[eo]rks\b|\bauto ?w[eo]rks?\b|\bauto ?worx\b|\bautotech\b|\bcar doctor\b", T_GENERAL, 0.55),
    (r"\bglass ?works\b|\bglass\b", T_GLASS, 0.5),
    (r"\brestoration\b|\bspeed shop\b|\bmachine shop\b|\bperformance\b|\bcustoms?\b|\bengines?\b", T_SPECIALTY, 0.5),
)
COMPOUND_STEMS: tuple[tuple[str, str], ...] = (
    ("collision", T_COLLISION), ("autobody", T_COLLISION), ("bodyshop", T_COLLISION),
    ("transmission", T_TRANSMISSION), ("brake", T_EXHAUST), ("exhaust", T_EXHAUST), ("muffler", T_EXHAUST),
    ("autorepair", T_GENERAL), ("mechanic", T_GENERAL), ("(?<!c)lube", T_LUBE), ("(?<!en)(?<!re)(?<!at)(?<!sa)tire", T_TIRE),
)
# Words that look like repair but are not vehicle repair
NAME_FALSE_REPAIR = re.compile(r"\bgarage doors?\b|\bappliance\b|\bphone repair\b|\bcomputer repair\b|\bshoe repair\b|\bhvac\b|\bair conditioning and heating\b")

NAME_DEALER_KEYWORDS: tuple[tuple[str, float], ...] = (
    (r"\bauto ?sales\b|\bcar sales\b|\bcars? for sale\b|\bmotor ?cars\b|\bauto ?mall\b|\bdealership\b|\bsuperstore\b|\bpre ?owned\b", 0.75),
    (r"\binternational trucks?\b|\b(?:truck|bus|trailer|coach) sales\b|\bauto ?group\b|\bautomotive group\b|\bmotor group\b|\bauto ?brokers?\b|\bauto ?outlet\b|\bcar outlet\b|\bauto ?plaza\b|\bused cars\b", 0.7),
    (r"\bbuy here pay here\b|\b(?:auto|car|vehicle) auctions?\b|\bauctions?\b|\bcar lot\b|\bauto ?exchange\b|\bcar ?trader\b", 0.7),
    (r"\bmotors\b|\bmotor company\b|\bautohaus\b|\bimports\b|\bcars\b", 0.5),
)

NAME_EXCLUDED_KEYWORDS: tuple[tuple[str, str, float], ...] = (
    (r"\bmarine\b|\bboats?\b|\byachts?\b|\boutboard\b|\bwatercraft\b", T_MARINE, 0.75),
    (r"\bgolf (?:carts?|cars?)\b|\bcarts\b", T_GOLF, 0.75),
    (r"\brv\b|\brvs\b|\bmotorhomes?\b|\bcampers?\b", T_RV, 0.7),
    (r"\bmotorsports?\b|\bpowersports\b|\bcycles\b", T_POWERSPORTS, 0.6),
    (r"\btowing\b|\btow\b|\bwrecker\b|\broadside\b", T_TOWING, 0.65),
    (r"\bcar wash\b|\bdetail(?:ing|ers?)?\b|\b(?:auto|car) spa\b|\bceramic\b|\bwash\b", T_WASH, 0.65),
    (r"\btint(?:ing|s)?\b|\bwraps?\b|\baudio\b|\bstereo\b|\bsound\b|\bremote start\b", T_TINT, 0.65),
    (r"\brentals?\b|\brent a car\b|\bleasing\b|\bcar sharing\b", T_RENTAL, 0.65),
    (r"\bparking\b|\bvalet\b|\bpark and (?:ride|fly)\b", T_PARKING, 0.7),
    (r"\bequipment\b|\btractors?\b|\bforklifts?\b|\bcranes?\b", T_EQUIPMENT, 0.55),
    (r"\btransport(?:ation|s)?\b|\btrucking\b|\blogistics\b|\bfreight\b|\btransit\b|\blimo(?:usine)?s?\b|\bmoving\b", T_TRANSPORT, 0.6),
    (r"\bcouriers?\b|\bhauling\b|\bshipping\b|\bshuttles?\b|\btaxis?\b|\bcabs?\b|\bcarriers?\b|\bbus (?:lines?|service|company)\b|\bdelivery\b|\bdispatch\b", T_TRANSPORT, 0.55),
    (r"\bparts\b", T_PARTS, 0.5),
    (r"\binsurance\b|\brealty\b|\breal estate\b|\bproperties\b|\btax\b|\baccounting\b|\bcpa\b|\blaw\b|\bmedical\b|\bdental\b", T_UNRELATED, 0.5),
)

# ====================================================================================== site patterns
# Repair service categories: (key, label, pattern)
REPAIR_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("brakes", "brakes", _rx(r"\bbrakes?\s+(?:repairs?|services?|replacement|inspections?|jobs?|work|systems?)\b", r"\bpads?\s+(?:and|&)\s+rotors?\b")),
    ("oil", "oil change", _rx(r"\boil\s+(?:changes?|and\s+filter|services?)\b", r"\blube\s+(?:services?|oil|and\s+filter)\b", r"\bquick\s+lube\b")),
    ("engine", "engine repair", _rx(r"\bengine\s+(?:repairs?|rebuild\w*|replacement|services?|work)\b", r"\bcheck\s+engine\s+light\b")),
    ("diagnostics", "diagnostics", _rx(r"\b(?:computer|vehicle|auto|engine|electrical|car)\s+diagnos\w*", r"\bdiagnostic\s+(?:services?|testing|scans?|checks?)\b")),
    ("transmission", "transmission", _rx(r"\btransmissions?\s+(?:repairs?|services?|rebuild\w*|flush|replacement|fluid)\b", r"\bclutch\s+(?:repairs?|replacement)\b")),
    ("ac", "A/C", _rx(r"\b(?:auto(?:motive)?|car|vehicle)\s+(?:a/?c|air\s+conditioning)\b", r"\ba/?c\s+(?:repairs?|services?|recharge|system)\b", r"\bair\s+conditioning\s+(?:repairs?|services?)\b")),
    ("suspension", "suspension & steering", _rx(r"\bsuspension\b", r"\bsteering\s+(?:repairs?|services?|systems?)\b", r"\bshocks?\s+(?:and|&)\s+struts?\b", r"\bstruts\b")),
    ("alignment", "alignment", _rx(r"\b(?:wheel|front[- ]end|4[- ]wheel|four[- ]wheel)\s+alignments?\b")),
    ("tires", "tire service", _rx(r"\btires?\s+(?:installation|install|mounting|balancing|rotations?|repairs?|services?|replacement|changes?)\b", r"\bflat\s+(?:tire\s+)?repairs?\b", r"\bmount\s+(?:and|&)\s+balance\b", r"\btire\s+(?:and|&)\s+(?:auto|wheel)\s+services?\b")),
    ("exhaust", "exhaust & muffler", _rx(r"\bmufflers?\b", r"\bexhaust\s+(?:repairs?|systems?|services?|work)\b", r"\bcatalytic\s+converters?\b")),
    ("cooling", "cooling system/radiator", _rx(r"\bradiators?\s+(?:repairs?|services?|replacement)\b", r"\bcooling\s+systems?\b", r"\bcoolant\s+(?:flush|services?)\b", r"\bwater\s+pumps?\b")),
    ("electrical", "electrical", _rx(r"\b(?:auto(?:motive)?|vehicle|car)\s+electrical\b", r"\belectrical\s+(?:repairs?|diagnos\w*|systems?\s+repairs?)\b", r"\bstarters?\s+(?:and|&)\s+alternators?\b", r"\balternators?\b")),
    ("maintenance", "scheduled maintenance", _rx(r"\btune[- ]?ups?\b", r"\b(?:scheduled|factory|preventive|preventative|routine)\s+maintenance\b", r"\b(?:30|60|90)[,k]?(?:000)?[- ]mile\s+services?\b", r"\bfluid\s+(?:flush(?:es)?|services?|exchanges?)\b", r"\btiming\s+belts?\b", r"\bspark\s+plugs\b")),
    ("collision", "collision/body repair", _rx(r"\bcollision\s+(?:repairs?|centers?|centres?|services?|shop|work|specialists?)\b", r"\bauto\s*body\s+(?:repairs?|shop|work|services?)\b", r"\bbody\s+shop\b", r"\bframe\s+(?:repairs?|straightening|machines?)\b", r"\bhail\s+(?:damage|repairs?)\b", r"\bdent\s+(?:repairs?|removal)\b", r"\bpaintless\s+dent\b", r"\bpaint\s+(?:and|&)\s+body\b", r"\bauto(?:motive)?\s+paint(?:ing)?\b", r"\bbumper\s+repairs?\b", r"\bi-?car\b")),
    ("glass", "auto glass", _rx(r"\bwindshield\s+(?:repairs?|replacement|installation)\b", r"\bauto\s+glass\b", r"\bchip\s+repairs?\b")),
    ("fleet", "diesel/truck/fleet repair", _rx(r"\bdiesel\s+(?:repairs?|services?|engine\s+repairs?|mechanics?|technicians?)\b", r"\b(?:truck|fleet|trailer|commercial\s+vehicle|heavy[- ]duty)\s+(?:repairs?|maintenance|services?)\b", r"\bdot\s+inspections?\b")),
    ("mobile", "mobile repair", _rx(r"\bmobile\s+(?:mechanics?|auto\s+repairs?|repair\s+services?|tire\s+services?|auto\s+services?)\b", r"\bwe\s+come\s+to\s+you\b")),
    ("general", "general auto repair", _rx(r"\b(?:auto(?:motive)?|car|vehicle|truck)\s+repairs?\b", r"\bauto\s+(?:mechanics?|service\s+centers?|care\s+centers?|repair\s+shop)\b", r"\b(?:general|complete)\s+(?:auto\s+)?(?:repairs?|car\s+care)\b", r"\bmechanical\s+repairs?\b", r"\b(?:auto|repair)\s+shop\b")),
    ("staff", "certified technicians/service bays", _rx(r"\b(?:certified|experienced|our|skilled)\s+(?:technicians|mechanics)\b", r"\base[- ]certified\b", r"\bservice\s+bays?\b")),
    ("inspection", "inspections", _rx(r"\b(?:state|vehicle|pre-?purchase|multi-?point|safety)\s+inspections?\b", r"\bemissions?\s+(?:tests?|testing|repairs?)\b")),
)
REPAIR_LABELS = {key: label for key, label, _ in REPAIR_PATTERNS}
# Categories whose words also appear outside the automotive world
WEAK_CATEGORIES = frozenset({"suspension", "inspection", "maintenance", "electrical", "diagnostics", "engine", "cooling", "ac", "staff"})
# One of these offered on a service page or the homepage is enough to be a repair business
SPECIALIST_CATEGORIES = frozenset({"general", "collision", "transmission", "glass", "fleet", "mobile", "tires", "oil", "exhaust", "brakes"})
# Business type implied by the dominant repair categories
CATEGORY_TYPES: dict[str, str] = {
    "collision": T_COLLISION, "glass": T_GLASS, "transmission": T_TRANSMISSION, "fleet": T_FLEET,
    "mobile": T_MOBILE, "tires": T_TIRE, "alignment": T_TIRE, "oil": T_LUBE, "exhaust": T_EXHAUST,
    "ac": T_AC, "cooling": T_AC,
}

# Dealership evidence: (key, weight, pattern)
DEALER_PATTERNS: tuple[tuple[str, float, re.Pattern[str]], ...] = (
    ("inventory", 1.5, _rx(r"\b(?:new|used|pre-?owned|certified)\s+(?:vehicle|car|truck|suv)s?\s+(?:inventory|for\s+sale|specials)\b", r"\b(?:search|view|browse|shop)\s+(?:our\s+|all\s+)?(?:new\s+|used\s+|pre-?owned\s+)?(?:inventory|vehicles)\b", r"\b(?:new|used|vehicle)\s+inventory\b")),
    ("new_used", 1.5, _rx(r"\bnew\s+(?:and|&)\s+(?:used|pre-?owned)\b", r"\bpre-?owned\s+(?:vehicles|cars|inventory)\b", r"\bcertified\s+pre-?owned\b", r"\bused\s+cars\s+for\s+sale\b", r"\bbuy\s+here,?\s+pay\s+here\b")),
    ("test_drive", 1.0, _rx(r"\btest\s+drives?\b")),
    ("trade", 1.0, _rx(r"\bvalue\s+your\s+trade\b", r"\btrade[- ]in\s+(?:value|appraisal|offer)\b", r"\bsell\s+(?:us\s+)?your\s+(?:car|vehicle)\b")),
    ("financing", 1.0, _rx(r"\b(?:auto|car|vehicle)\s+(?:financing|loans?)\b", r"\bcredit\s+application\b", r"\bget\s+pre-?approved\b", r"\bpayment\s+calculator\b", r"\bfinance\s+(?:center|department)\b")),
    ("pricing", 1.0, _rx(r"\bmsrp\b", r"\bstock\s*#", r"\blease\s+(?:specials|offers|deals)\b", r"\bshowroom\b")),
    ("platform", 1.5, _rx(r"\bdealer\s?inspire\b", r"\bdealer\.com\b", r"\bdealeron\b", r"\bdealer\s?fire\b", r"\bdealer\s?socket\b", r"\bdealer\s+e-?process\b")),
    ("dealer_word", 0.75, _rx(r"\bdealership\b", r"\bauthorized\s+[a-z-]+\s+dealer\b", r"\b(?:new|used)\s+car\s+dealer\b", r"\bsales\s+department\b")),
)
# Repair shops compare themselves with dealers: "skip the dealership", "dealer-quality service"
DEALER_COMPARISON = re.compile(
    r"\b(?:than|vs\.?|versus|instead\s+of|alternative|without|skip|like\s+the|dealer[- ](?:quality|level)|dealer\s+prices|save)\b",
    re.IGNORECASE,
)

# Excluded trades and non-road vehicles: (type, pattern)
EXCLUSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (T_PARTS, _rx(r"\b(?:used|quality|oem|aftermarket)\s+(?:auto\s+|truck\s+)?parts\b", r"\bsalvage\s+yard\b", r"\bjunk\s*yard\b", r"\bdismantl\w+\b", r"\bauto\s+parts\s+(?:stores?|retailers?)\b", r"\bshop\s+(?:auto\s+)?parts\b", r"\badd\s+to\s+cart\b", r"\bbuy\s+online\b", r"\bin[- ]store\s+pick\s?up\b", r"\bfree\s+(?:battery\s+testing|loaner\s+tools)\b")),
    (T_TOWING, _rx(r"\btowing\b", r"\btow\s+trucks?\b", r"\bwreckers?\b", r"\broadside\s+assistance\b", r"\bflatbed\b", r"\bjump\s+starts?\b", r"\block\s?outs?\b")),
    (T_WASH, _rx(r"\bcar\s+wash\b", r"\bdetailing\b", r"\bauto\s+detail\b", r"\bceramic\s+coating\b", r"\bpaint\s+correction\b", r"\bunlimited\s+wash\b")),
    (T_TINT, _rx(r"\bwindow\s+tint\w*\b", r"\b(?:vinyl|vehicle|car)\s+wraps?\b", r"\bcar\s+audio\b", r"\bpaint\s+protection\s+film\b", r"\bclear\s+bra\b", r"\bremote\s+start\w*\b", r"\bmobile\s+electronics\b")),
    (T_RENTAL, _rx(r"\bcar\s+rentals?\b", r"\brent\s+a\s+(?:car|truck)\b", r"\btruck\s+(?:rental|leasing)\b", r"\bvehicle\s+(?:rental|leasing)\b")),
    (T_PARKING, _rx(r"\bparking\s+(?:garages?|lots?\s+(?:management|operator|services?)|facilit\w+|rates|reservations?|services?|management|operators?)\b", r"\bvalet\s+(?:parking|services?)\b", r"\b(?:monthly|daily|hourly|airport|event|cruise\s+port)\s+parking\b", r"\bpark\s+(?:and|&)\s+(?:fly|ride)\b", r"\breserve\s+(?:a\s+|your\s+)?parking\b")),
    (T_MARINE, _rx(r"\bboats?\b", r"\bmarine\b", r"\byachts?\b", r"\boutboards?\b", r"\bpontoons?\b", r"\bjet\s*skis?\b", r"\bwatercraft\b")),
    (T_GOLF, _rx(r"\bgolf\s+carts?\b", r"\bgolf\s+cars?\b", r"\blow[- ]speed\s+vehicles?\b")),
    (T_RV, _rx(r"\brv\s+(?:dealer\w*|sales|inventory|for\s+sale|service)\b", r"\bmotorhomes?\b", r"\btravel\s+trailers?\b", r"\bfifth\s+wheels?\b", r"\bcampers?\b")),
    (T_POWERSPORTS, _rx(r"\bpowersports\b", r"\batvs?\b", r"\butvs?\b", r"\bside[- ]by[- ]sides?\b", r"\bdirt\s+bikes?\b", r"\bjet\s+boats?\b")),
    (T_EQUIPMENT, _rx(r"\bheavy\s+equipment\b", r"\bconstruction\s+equipment\b", r"\bexcavators?\b", r"\bforklifts?\b", r"\bcaterpillar\b", r"\bcat\s+dealer\b", r"\bpower\s+generation\b")),
    (T_TRANSPORT, _rx(r"\bfreight\b", r"\blogistics\b", r"\btrucking\s+(?:company|services?)\b", r"\bcdl\s+drivers?\b", r"\bowner[- ]operators?\b", r"\bpublic\s+transit\b", r"\btransportation\s+services\b", r"\b(?:auto|car|vehicle)\s+(?:transport|shipping)\b", r"\bmoving\s+services\b", r"\bdispatch\b")),
)
NON_ROAD_TYPES = frozenset({T_MARINE, T_GOLF, T_RV, T_POWERSPORTS, T_EQUIPMENT})
# Trades whose sites need not be car-vocabulary dense (a freight or parking site rarely talks about cars):
# they count when the homepage title / description names the trade
SELF_EVIDENT_TRADES: dict[str, re.Pattern[str]] = {
    T_TRANSPORT: re.compile(r"\b(?:transport\w*|trucking|logistics|freight|moving|movers|shipping|hauling|courier|limo\w*|shuttle|taxi|dispatch)\b"),
    T_PARKING: re.compile(r"\b(?:parking|valet)\b"),
    T_RENTAL: re.compile(r"\b(?:rentals?|rent a|leasing)\b"),
}

# Industries of unrelated sites (labels for the reason; the decision only needs "not automotive")
UNRELATED_INDUSTRIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("accounting/tax/advisory firm", _rx(r"\baudit\b", r"\btax\s+(?:services|preparation|advisory|returns?)\b", r"\bcpas?\b", r"\baccounting\b", r"\bbookkeeping\b", r"\badvisory\b")),
    ("investment/finance firm", _rx(r"\binvestments?\b", r"\bwealth\s+management\b", r"\bprivate\s+equity\b", r"\basset\s+management\b", r"\bventure\s+capital\b", r"\bmortgage\b")),
    ("real estate business", _rx(r"\breal\s+estate\b", r"\brealtors?\b", r"\bproperty\s+management\b", r"\bhomes\s+for\s+sale\b")),
    ("insurance agency", _rx(r"\binsurance\s+(?:agency|agents?|quotes?|policies|carrier)\b")),
    ("law firm", _rx(r"\battorneys?\b", r"\blaw\s+firm\b", r"\blawyers?\b")),
    ("IT/marketing/web services company", _rx(r"\bsoftware\b", r"\bit\s+services\b", r"\bweb\s+(?:design|hosting)\b", r"\bdigital\s+marketing\b", r"\bseo\b", r"\bmanaged\s+services\b")),
    ("construction/home-services company", _rx(r"\broofing\b", r"\bplumbing\b", r"\bhvac\b", r"\bremodel\w*\b", r"\bgeneral\s+contractor\b", r"\blandscaping\b")),
    ("healthcare provider", _rx(r"\bpatients?\b", r"\bclinic\b", r"\bdental\b", r"\bphysicians?\b")),
)

AUTOMOTIVE_VOCAB = re.compile(
    r"\b(?:auto|automotive|autos|cars?|vehicles?|trucks?|tires?|brakes?|engines?|mechanics?|collision|transmissions?|"
    r"oil\s+change|dealership|suvs?|fleet|motors?|wheels?|windshields?|mufflers?)\b",
    re.IGNORECASE,
)

NEGATION = re.compile(r"\b(?:not|don'?t|do\s+not|does\s+not|doesn'?t|never|nor|no\s+longer)\b[^.|]{0,40}$", re.IGNORECASE)

SERVICE_PAGE = re.compile(
    r"repair|service|maintenance|collision|brake|tire|oil|transmission|diagnos|alignment|glass|body|mechanic|a-?c\b|exhaust|muffler|fleet",
    re.IGNORECASE,
)
INFO_PAGE = re.compile(r"about|contact|location|direction|hours|team|staff|who-we-are|our-story", re.IGNORECASE)
EDITORIAL_PAGE = re.compile(r"/(?:blog|news|articles?|posts?|press|tips|\d{4}/\d{2})(?:/|$)", re.IGNORECASE)
INVENTORY_PAGE = re.compile(r"inventory|new-vehicles|used-vehicles|pre-owned|vehicles-for-sale|/vdp|/srp", re.IGNORECASE)
ARCHIVE_PATH_PREFIX = re.compile(r"^/web/\d+[a-z_]*/(?:https?:/+[^/]+)?", re.IGNORECASE)


# ============================================================================================ results
@dataclass
class NameSignal:
    """What the business / dba name alone says."""

    verdict: str = ""  # Yes | No | "" (no signal)
    business_type: str = T_UNKNOWN
    strength: float = 0.0  # 0..1
    terms: list[str] = field(default_factory=list)
    brand: str = ""  # known chain matched (repair or non-repair)
    source: str = ""  # "dba name" | "business name"

    @property
    def is_known_chain(self) -> bool:
        return bool(self.brand)


@dataclass
class Evidence:
    key: str
    label: str
    url: str
    snippet: str
    weight: float


@dataclass
class SiteProfile:
    """What a crawled website says about the business it represents (independent of any input row)."""

    accessible: bool
    site_type: str = T_UNKNOWN  # one of the T_* types, or T_UNRELATED / T_UNKNOWN
    verdict: str = ""  # Yes | No | ""
    strength: float = 0.0  # 0..1 confidence of the site's own verdict
    repair: dict[str, Evidence] = field(default_factory=dict)  # best evidence per repair category
    repair_score: float = 0.0
    dealer: dict[str, Evidence] = field(default_factory=dict)
    dealer_score: float = 0.0
    franchise_brand: str = ""
    exclusions: dict[str, list[Evidence]] = field(default_factory=dict)
    automotive_mentions: int = 0
    word_count: int = 0
    industry: str = ""  # label for unrelated sites
    summary: str = ""
    pages: int = 0
    text_norm: str = ""  # normalized site text (industry detection)

    @property
    def auto_dense(self) -> bool:
        """Car vocabulary is a real theme of the site, not a passing mention on a big corporate site."""
        if self.automotive_mentions < 5:
            return False
        return self.automotive_mentions * 1000 >= 4 * max(1, self.word_count)

    @property
    def is_automotive(self) -> bool:
        """The site represents an automotive (or excluded-vehicle-trade) business."""
        return bool(self.verdict) or self.auto_dense

    def evidence_urls(self, limit: int = 3) -> list[str]:
        items: list[Evidence] = []
        if self.verdict == YES:
            items = sorted(self.repair.values(), key=lambda e: -e.weight)
        elif self.site_type in (T_DEALER,):
            items = sorted(self.dealer.values(), key=lambda e: -e.weight)
        elif self.site_type in self.exclusions:
            items = self.exclusions[self.site_type]
        return list(dict.fromkeys(e.url for e in items if e.url))[:limit]


@dataclass
class AutoRepairClassificationResult:
    """Verdict for one lead: does the business provide vehicle repair / maintenance services?"""

    classification: str  # output bucket: Yes | No | Uncertain (needs review) | Unreachable
    decision_label: str  # Yes | No (always decided; written to "Is Auto Repair ?")
    confidence: float
    website_status: str  # OK | Timeout | DNS Failure | Parked Domain | Archived Copy (...) | Missing URL ...
    reasoning: str = ""
    short_reason: str = ""  # one line for the review report
    business_type: str = T_UNKNOWN
    services_found: list[str] = field(default_factory=list)
    decision_basis: str = ""  # Website | Website + Name | Name Only | Registry Default ...
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
    def needs_review(self) -> bool:
        return self.classification in ("Uncertain", "Unreachable")

    @property
    def borderline(self) -> bool:
        """Low confidence, or the name and website pointed different ways: worth a human look."""
        basis = self.decision_basis
        return self.confidence < BORDERLINE_THRESHOLD or any(w in basis for w in ("conflict", "disagree", "also sells"))

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutoRepairClassificationResult":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class LeadInfo:
    """The row values the classifier uses (name signals + registry default)."""

    business_name: str = ""
    dba_name: str = ""
    registry: str = ""  # e.g. license type "Motor Vehicle Repair"


CHALLENGE_MARKERS: tuple[str, ...] = (
    "checking your browser", "sgcaptcha", "just a moment...", "attention required", "verify you are human",
    "please enable cookies", "ddos protection", "access denied", "bot challenge required", "are you a robot",
)
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "coming soon", "under construction", "web hosting by", "this page belongs to a member", "default web site page",
    "default page", "index of /", "future home of", "launching soon", "account suspended", "this account has been suspended",
    "site not found", "website is under maintenance", "domain has been registered", "congratulations! your",
    "nothing here yet", "site is being built",
)


def unusable_site_reason(crawl: "CrawlResult") -> tuple[str, str]:
    """(status, message) when an 'accessible' crawl holds no business content (bot wall, placeholder, empty)."""
    if not crawl.is_accessible:
        return "", ""
    pages = crawl.all_pages
    text = " ".join(f"{p.title} {p.meta_description} {p.text_content}" for p in pages).lower() or crawl.combined_text.lower()
    words = sum(1 for w in text.split() if w != "|")
    head = text[:800]
    if words < 300 and any(m in head for m in CHALLENGE_MARKERS):
        return "Bot Challenge", "Website returned a bot-protection / browser-check page (not bypassed)"
    if words < 200 and any(m in text for m in PLACEHOLDER_MARKERS):
        return "Placeholder Page", "Website is a placeholder (coming soon / default hosting page); no business content"
    if words < 15:
        return "Empty Page", "Website returned almost no readable content"
    return "", ""


def downgrade_unusable(crawl: "CrawlResult") -> "CrawlResult":
    """Mark bot walls / placeholders / empty pages as inaccessible so they are never read as a business site."""
    status, message = unusable_site_reason(crawl)
    if not status:
        return crawl
    return replace(crawl, is_accessible=False, status_category=status, error_message=message)


# ============================================================================================ helpers
def _page_path(url: str) -> str:
    path = urlparse(url).path or "/"
    return ARCHIVE_PATH_PREFIX.sub("", path) or "/"


def _page_weight(url: str, title: str, is_home: bool) -> float:
    path = _page_path(url)
    if EDITORIAL_PAGE.search(path):
        return 0.35
    if is_home or path.strip("/") == "":
        return 1.0
    if SERVICE_PAGE.search(path) or SERVICE_PAGE.search(title or ""):
        return 1.0
    if INVENTORY_PAGE.search(path):
        return 0.9
    if INFO_PAGE.search(path):
        return 0.8
    return 0.7


def _snippet(text: str, start: int, end: int, radius: int = 70) -> str:
    s = max(0, start - radius)
    e = min(len(text), end + radius)
    return " ".join(text[s:e].replace("|", " ").split())


def _negated(text: str, start: int) -> bool:
    return bool(NEGATION.search(text[max(0, start - 60) : start]))


def _oem_in(text: str) -> str:
    """First OEM brand in normalized text (ambiguous brands need a dealer cue nearby)."""
    for m in _OEM_RE.finditer(text):
        brand = m.group(1)
        if brand in _AMBIGUOUS_OEM:
            window = text[max(0, m.start() - 40) : m.end() + 40]
            if not re.search(r"\b(?:dodge|chrysler|jeep|cooper|dealer|of|motors|cars)\b", window.replace(brand, "", 1)):
                continue
        return brand
    return ""


# ======================================================================================= name analysis
def _match_chain(text: str) -> tuple[str, str, str]:
    """(verdict, business type, brand) for a known chain in normalized text."""
    for pattern, btype in REPAIR_CHAINS:
        m = re.search(pattern, text)
        if m:
            return YES, btype, m.group(0)
    for pattern, btype in NON_REPAIR_CHAINS:
        m = re.search(pattern, text)
        if m:
            return NO, btype, m.group(0)
    return "", "", ""


def _analyze_one_name(raw: str) -> NameSignal:
    text = _norm(clean_company_name(raw) or raw)
    if not text:
        return NameSignal()

    verdict, btype, brand = _match_chain(text)
    if verdict:
        return NameSignal(verdict, btype, 0.9, [brand], brand)

    repair_hits: list[tuple[str, float, str]] = []
    if not NAME_FALSE_REPAIR.search(text):
        for pattern, rtype, weight in NAME_REPAIR_KEYWORDS:
            m = re.search(pattern, text)
            if m:
                repair_hits.append((rtype, weight, m.group(0)))
    excluded_hits: list[tuple[str, float, str]] = []
    for pattern, etype, weight in NAME_EXCLUDED_KEYWORDS:
        m = re.search(pattern, text)
        if m:
            excluded_hits.append((etype, weight, m.group(0)))
    dealer_hits: list[tuple[float, str]] = []
    for pattern, weight in NAME_DEALER_KEYWORDS:
        m = re.search(pattern, text)
        if m:
            dealer_hits.append((weight, m.group(0)))

    oem = _oem_in(text)
    # "Toyota of Orlando", "Lipton Toyota", "Orlando Dodge Chrysler Jeep Ram": OEM brand without repair words
    strong_repair = [h for h in repair_hits if h[1] >= 0.7 and h[0] != T_SPECIALTY]
    oem_dealer = bool(oem) and (re.search(rf"\b{re.escape(oem)} of\b", text) or not strong_repair)

    if oem_dealer:
        terms = [oem] + [t for _, t in dealer_hits]
        if strong_repair and re.search(r"\bcollision\b|\bbody\b|\bservice center\b", text):
            return NameSignal(NO, T_DEALER_OWNED, 0.75, terms + [strong_repair[0][2]])
        return NameSignal(NO, T_DEALER, 0.85, terms)

    if strong_repair:
        best = max(strong_repair, key=lambda h: h[1])
        # Specific trades outrank the generic "auto repair" bucket ("Joe's Auto Body" is collision)
        specific = [h for h in strong_repair if h[0] != T_GENERAL]
        btype = (specific[0] if specific else best)[0]
        weight = best[1]
        terms = [h[2] for h in strong_repair]
        # "Towing & Auto Repair", "Tint & Collision": genuinely offering repair makes it a repair business
        if dealer_hits and not any(h[0] != T_GENERAL for h in strong_repair) and max(w for w, _ in dealer_hits) >= 0.7:
            # "Auto Sales & Service": mixed; the website decides, the name leans repair only weakly
            return NameSignal(YES, T_GENERAL, 0.45, terms + [t for _, t in dealer_hits])
        return NameSignal(YES, btype, weight, terms)

    if dealer_hits:
        weight, term = max(dealer_hits)
        return NameSignal(NO, T_DEALER, weight, [term])

    if excluded_hits:
        etype, weight, term = max(excluded_hits, key=lambda h: h[1])
        # "Piper Automotive and Marine Service": cars are serviced too; the website decides
        if etype in NON_ROAD_TYPES and re.search(r"\bauto(?:motive)?\b", text) and re.search(r"\b(?:service|repair)s?\b", text):
            return NameSignal(YES, T_GENERAL, 0.45, [term, "automotive service"])
        return NameSignal(NO, etype, weight, [term])

    if repair_hits:  # specialty-only words ("performance", "customs", "engines")
        rtype, weight, term = max(repair_hits, key=lambda h: h[1])
        return NameSignal(YES, rtype, weight, [term])

    # Trade words fused into a brand ("NuBrakes", "GExhaust", "AutoBodyPros")
    squashed = text.replace(" ", "")
    for stem, stype in COMPOUND_STEMS:
        if re.search(stem, squashed):
            return NameSignal(YES, stype, 0.6, [stem])

    if re.search(r"\bauto\b|\bautos\b|\bcar\b|\bvehicle\b", text):
        return NameSignal(YES, T_GENERAL, 0.35, ["auto"])
    return NameSignal()


_JUNK_PART = re.compile(r"[{}=<>\\]|\bfunction\b|\bvar\b|document\.|getelementbyid|\breturn\b|\bwindow\.", re.IGNORECASE)


def clean_dba(raw: str) -> str:
    """Trading name(s) from a dba cell; stops at scraped page junk ("Maus Nissan; 1; function validateInput() {...")."""
    keep: list[str] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or part.isdigit():
            continue
        if _JUNK_PART.search(part) or len(part) > 90:
            break
        keep.append(part)
        if len(keep) >= 3:
            break
    return "; ".join(keep)


def analyze_name(business_name: str, dba_name: str = "") -> NameSignal:
    """Name verdict: the dba (trading name) outranks the legal name, which is often a holding company."""
    dba_name = clean_dba(dba_name)
    dba = _analyze_one_name(dba_name) if dba_name else NameSignal()
    legal = _analyze_one_name(business_name) if business_name else NameSignal()
    if dba.verdict:
        dba.source = "dba name"
        if legal.verdict == dba.verdict:
            dba.strength = min(0.95, dba.strength + 0.05)
            dba.terms = list(dict.fromkeys(dba.terms + legal.terms))
            if legal.brand and not dba.brand:
                dba.brand = legal.brand
        elif legal.verdict and legal.is_known_chain and not dba.is_known_chain:
            legal.source = "business name"
            return legal
        return dba
    legal.source = "business name" if legal.verdict else ""
    return legal


# ====================================================================================== site analysis
def profile_site(crawl: "CrawlResult") -> SiteProfile:
    """Profile a crawled website: repair services, dealership signals, excluded trades, industry."""
    if not crawl.is_accessible:
        return SiteProfile(accessible=False)

    profile = SiteProfile(accessible=True)
    pages = crawl.all_pages
    profile.pages = len(pages)
    home = crawl.homepage
    texts: list[str] = []

    for page in pages:
        is_home = page is home
        weight = _page_weight(page.url, page.title, is_home)
        text = f"{page.title} | {page.meta_description} | {page.text_content}"
        texts.append(text)
        lower = text.lower()

        for key, label, pattern in REPAIR_PATTERNS:
            for m in pattern.finditer(lower):
                if _negated(lower, m.start()):
                    continue
                current = profile.repair.get(key)
                if current is None or weight > current.weight:
                    profile.repair[key] = Evidence(key, label, page.url, _snippet(text, m.start(), m.end()), weight)
                break

        for key, dweight, pattern in DEALER_PATTERNS:
            for m in pattern.finditer(lower):
                if key == "dealer_word" and DEALER_COMPARISON.search(lower[max(0, m.start() - 50) : m.end() + 30]):
                    continue
                if _negated(lower, m.start()):
                    continue
                current = profile.dealer.get(key)
                if current is None or weight > current.weight:
                    profile.dealer[key] = Evidence(key, key.replace("_", " "), page.url, _snippet(text, m.start(), m.end()), dweight * weight)
                break

        for etype, pattern in EXCLUSION_PATTERNS:
            hits = [m for m in pattern.finditer(lower) if not _negated(lower, m.start())]
            if hits:
                m = hits[0]
                profile.exclusions.setdefault(etype, []).append(
                    Evidence(etype, m.group(0), page.url, _snippet(text, m.start(), m.end()), weight * min(3, len(hits)))
                )

        profile.automotive_mentions += len(AUTOMOTIVE_VOCAB.findall(lower))
        profile.word_count += sum(1 for w in text.split() if w != "|")

    # Generic service words ("suspension", "inspections", "maintenance") only mean vehicle repair on a
    # site that is actually about vehicles
    if not profile.auto_dense:
        for key in WEAK_CATEGORIES:
            profile.repair.pop(key, None)

    profile.text_norm = _norm(" ".join(texts))

    profile.repair_score = round(sum(e.weight for e in profile.repair.values()), 2)
    profile.dealer_score = round(sum(e.weight for e in profile.dealer.values()), 2)

    # Franchise dealer: OEM brand in the homepage title / description / domain plus dealer evidence
    home_head = _norm(f"{home.title} {home.meta_description}") if home else ""
    oem = _oem_in(f"{home_head} {domain_slug(crawl.domain)}") or _oem_in(home_head)
    if not oem:
        slug = domain_slug(crawl.domain)
        oem = next((b for b in OEM_BRANDS if len(b) >= 4 and b.replace(" ", "") in slug), "")
    # ("Honda & Acura Repair Specialists" names an OEM brand but is an independent shop)
    if oem and profile.dealer_score >= 1.5 and not re.search(rf"\b{re.escape(oem)}\b[a-z ]{{0,30}}\b(?:repair|specialists?)\b", home_head):
        profile.franchise_brand = oem

    _decide_site_type(profile, home_head)
    return profile


def _exclusion_strength(profile: SiteProfile, etype: str) -> float:
    return sum(e.weight for e in profile.exclusions.get(etype, []))


def _decide_site_type(profile: SiteProfile, home_head: str) -> None:
    specialist = [k for k, e in profile.repair.items() if k in SPECIALIST_CATEGORIES and e.weight >= 0.9]
    concrete = [k for k in profile.repair if k not in WEAK_CATEGORIES]
    # Generic wording alone ("certified technicians", "routine maintenance") never makes a repair business
    strong_repair = bool(specialist) or (profile.repair_score >= 2.0 and len(concrete) >= 1)
    dealer_kinds = {k for k in profile.dealer if k != "dealer_word"}
    is_dealer = profile.dealer_score >= 2.5 and len(dealer_kinds) >= 2

    # Non-road vehicles (boats, golf carts, RVs, powersports, heavy equipment) dominate an "automotive" read
    non_road = max(((t, _exclusion_strength(profile, t)) for t in NON_ROAD_TYPES), key=lambda x: x[1])
    road_mentions = profile.automotive_mentions
    if non_road[1] >= 3.0 and non_road[1] * 4 >= road_mentions:
        _set(profile, non_road[0], NO, 0.85, f"{non_road[0].lower()} business (non-road vehicles)")
        return

    if profile.franchise_brand or is_dealer:
        head_repair = bool(re.search(r"\b(?:repair|collision|body shop|mechanic|auto service|tires?|brakes?)\b", home_head))
        head_sales = bool(re.search(r"\b(?:dealer\w*|new|used|pre owned|inventory|sales|cars for sale)\b", home_head))
        if not profile.franchise_brand and strong_repair and head_repair and not head_sales:
            _set(profile, T_GENERAL, YES, 0.75, "repair shop that also sells vehicles")
            return
        brand = f"{profile.franchise_brand.title()} " if profile.franchise_brand else ""
        kinds = ", ".join(sorted(dealer_kinds)) or "dealer wording"
        _set(profile, T_DEALER, NO, 0.9 if (profile.franchise_brand or profile.dealer_score >= 4) else 0.8, f"{brand}dealership ({kinds.replace('_', ' ')})")
        return

    # Excluded trades (only when repair is not genuinely offered)
    trade_scores = [(t, _exclusion_strength(profile, t)) for t in profile.exclusions if t not in NON_ROAD_TYPES]
    trade_scores.sort(key=lambda x: -x[1])

    if strong_repair:
        mixed = f" alongside {trade_scores[0][0].lower()}" if trade_scores and trade_scores[0][1] >= 3 else ""
        _set(profile, _repair_type(profile), YES, 0.9 if profile.repair_score >= 3.0 else 0.8, f"repair services listed{mixed}")
        return
    self_evident = [
        (t, score) for t, score in trade_scores
        if t in SELF_EVIDENT_TRADES and score >= 2.0 and SELF_EVIDENT_TRADES[t].search(home_head)
    ]
    if self_evident and not specialist and not profile.auto_dense:
        etype, score = self_evident[0]
        _set(profile, etype, NO, 0.8 if score >= 3 else 0.7, f"{etype.lower()} business (no repair services found)")
        return
    if trade_scores and trade_scores[0][1] >= 1.5 and not specialist and profile.auto_dense:
        etype = trade_scores[0][0]
        if etype == T_PARTS and concrete:
            pass  # parts store with some service language: let the name decide
        else:
            _set(profile, etype, NO, 0.8 if trade_scores[0][1] >= 3 else 0.7, f"{etype.lower()} (no repair services found)")
            return

    if concrete and profile.repair_score >= 0.7:
        _set(profile, _repair_type(profile), YES, 0.65, "some repair services mentioned")
        return

    if profile.auto_dense:
        _set(profile, T_UNKNOWN, "", 0.0, "automotive site without clear services")
        return

    industry = ""
    best = 0
    for label, pattern in UNRELATED_INDUSTRIES:
        n = len(pattern.findall(profile.text_norm))
        if n > best:
            industry, best = label, n
    profile.industry = industry if best >= 2 else ""
    _set(profile, T_UNRELATED, "", 0.0, profile.industry or "non-automotive website")


def _repair_type(profile: SiteProfile) -> str:
    cats = profile.repair
    # A shop listing many general services is a general repair shop even if it also does tires
    general_breadth = len(set(cats) - {"collision", "glass", "fleet", "mobile"})
    for key in ("collision", "glass", "transmission", "fleet", "mobile"):
        if key in cats and cats[key].weight >= 0.9 and general_breadth < 4:
            return CATEGORY_TYPES[key]
    if general_breadth >= 4 or "general" in cats:
        return T_GENERAL
    for key in ("tires", "oil", "exhaust", "ac", "cooling", "alignment"):
        if key in cats:
            return CATEGORY_TYPES[key]
    return T_GENERAL


def _a(phrase: str) -> str:
    """Phrase with its indefinite article ("an accounting firm", "a dealership")."""
    return f"{'an' if phrase[:1].lower() in 'aeiou' else 'a'} {phrase}"


def _set(profile: SiteProfile, site_type: str, verdict: str, strength: float, summary: str) -> None:
    profile.site_type = site_type
    profile.verdict = verdict
    profile.strength = strength
    profile.summary = summary


# ========================================================================================= classifier
# A non-automotive site only proves "not auto repair" when it has real content
MIN_WORDS_FOR_INDUSTRY = 150


@dataclass
class _Decision:
    verdict: str
    confidence: float
    business_type: str
    basis: str
    reason: str
    short: str


class AutoRepairClassifier:
    """Fuses the name and the website profile into a Yes/No verdict with confidence and reasons.

    The website on the row is taken as the business's own site (no ownership verification).
    """

    def classify(
        self,
        crawl: "CrawlResult | None",
        lead: LeadInfo,
        profile: SiteProfile | None = None,
        archived_snapshot: str = "",
        live_status: str = "",
    ) -> AutoRepairClassificationResult:
        name = analyze_name(lead.business_name, lead.dba_name)

        if crawl is None:
            profile = SiteProfile(accessible=False)
            website_status = "Missing URL"
        else:
            crawl = downgrade_unusable(crawl)
            if profile is None or profile.accessible != crawl.is_accessible:
                profile = profile_site(crawl)
            website_status = crawl.status_category or ("OK" if crawl.is_accessible else "Connection Error")

        d = self._fuse(name, profile, lead, crawl)
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

        site_used = d.basis.startswith("Website")
        services = [REPAIR_LABELS[k] for k in sorted(profile.repair, key=lambda k: -profile.repair[k].weight)]
        phrases = (
            [e.snippet[:120] for e in sorted(profile.repair.values(), key=lambda e: -e.weight)[:3]]
            if site_used and d.verdict == YES
            else [e.snippet[:120] for e in list(profile.dealer.values())[:3]]
            if site_used and d.business_type in (T_DEALER, T_DEALER_OWNED)
            else []
        )
        return AutoRepairClassificationResult(
            classification=bucket,
            decision_label=d.verdict,
            confidence=conf,
            website_status=website_status,
            reasoning=reason,
            short_reason=f"{d.verdict}: {short}",
            business_type=d.business_type,
            services_found=services if profile.is_automotive else [],
            decision_basis=d.basis,
            evidence_urls=profile.evidence_urls() if site_used else [],
            evidence_phrases=phrases,
            access_error=(crawl.error_message or "") if crawl is not None and not crawl.is_accessible else "",
            pages_evaluated=profile.pages,
            crawl_details=_crawl_details(crawl) if crawl is not None else "",
            site_accessible=profile.accessible,
        )

    # ------------------------------------------------------------------ fusion
    def _fuse(self, name: NameSignal, site: SiteProfile, lead: LeadInfo, crawl: "CrawlResult | None") -> _Decision:
        domain = crawl.domain if crawl is not None else ""
        name_desc = self._name_desc(name)
        name_short = self._name_short(name)

        # 1. The website gives its own automotive verdict (repair shop, dealership, excluded trade)
        if site.accessible and site.verdict:
            return self._fuse_with_site_verdict(name, site, name_desc, name_short)

        # 2. A substantial non-automotive website (accountant, investor, realtor, ...)
        if site.accessible and site.site_type == T_UNRELATED and site.word_count >= MIN_WORDS_FOR_INDUSTRY:
            what = site.industry or "non-automotive business"
            site_note = f"Website {domain} is {_a(what)} site with no vehicle services."
            if name.verdict == YES and (name.is_known_chain or name.strength >= 0.7):
                # e.g. a tire chain whose email (and so website) is its accountant's: the trade name wins,
                # but the conflict is flagged for review
                conf = 0.75 if name.is_known_chain else 0.5
                return _Decision(
                    YES, conf, name.business_type, "Name (website conflicts)",
                    f"{site_note} But the name ({name_desc}) clearly indicates vehicle repair, so the website is "
                    "probably an email/parent-company domain rather than the shop itself. Review recommended.",
                    f"name {name_short}; but website {domain} is {_a(what)}",
                )
            if name.verdict == YES:
                return _Decision(
                    YES, min(0.5, 0.15 + 0.5 * name.strength), name.business_type, "Name (website conflicts)",
                    f"{site_note} The name ({name_desc}) weakly suggests vehicle repair. Low confidence; review manually.",
                    f"weak name {name_short}; website {domain} is {_a(what)}",
                )
            if name.verdict == NO:
                return _Decision(
                    NO, 0.85, name.business_type if name.business_type != T_UNKNOWN else T_UNRELATED, "Website + Name",
                    f"{site_note} Name agrees ({name_desc}).",
                    f"website {domain} is {_a(what)}; name {name_short}",
                )
            if site.industry:
                return _Decision(
                    NO, 0.75, T_UNRELATED, "Website",
                    f"{site_note} The business's site is not automotive; no vehicle repair services.",
                    f"website {domain} is {_a(what)}, not automotive",
                )
            return _Decision(
                NO, 0.5, T_UNRELATED, "Website (no automotive content)",
                f"{site_note} The site's business could not be identified and the name gives no trade signal. "
                "Low confidence; review manually.",
                f"website {domain} has no automotive content; no trade word in the name",
            )

        # 3. An automotive website that lists no clear services (thin or navigation-only content)
        if site.accessible and site.is_automotive:
            what = f"Website {domain} is automotive but lists no clear services ({site.pages} page(s))."
            if name.verdict:
                if name.is_known_chain:
                    conf = 0.85
                else:
                    conf = min(0.75, 0.4 + 0.35 * name.strength)
                return _Decision(
                    name.verdict, conf, name.business_type, "Name + automotive site",
                    f"{what} Classified from the name ({name_desc}).",
                    f"name {name_short}; automotive website without a service list",
                )
            return _Decision(
                YES, 0.4, T_UNKNOWN, "Automotive site + registry",
                f"{what} The name gives no trade signal; {self._registry_note(lead)} Review manually.",
                "automotive website without a service list; no trade word in the name",
            )

        # 4. No usable website: decide from the name
        why, why_short = self._why_site_unusable(site, domain, crawl)
        if name.verdict:
            if name.is_known_chain:
                conf, basis = 0.8, "Name Only (known chain)"
            else:
                conf, basis = min(0.55, 0.15 + 0.5 * name.strength), "Name Only"
            return _Decision(
                name.verdict, conf, name.business_type, basis,
                f"{why} Classified from the name: {name_desc}." + ("" if name.is_known_chain else " Low confidence; review manually."),
                f"name {name_short} ({why_short})",
            )
        if "repair" in lead.registry.lower():
            return _Decision(
                YES, 0.3, T_UNKNOWN, "Registry Default",
                f"{why} The name gives no trade signal; {self._registry_note(lead)} Review manually.",
                f"registered as '{lead.registry}'; no trade word in the name ({why_short})",
            )
        return _Decision(
            NO, 0.2, T_UNKNOWN, "No Evidence",
            f"{why} The name gives no trade signal. Review manually.",
            f"no evidence of vehicle repair ({why_short})",
        )

    def _fuse_with_site_verdict(self, name: NameSignal, site: SiteProfile, name_desc: str, name_short: str) -> _Decision:
        site_desc = self._site_desc(site)
        site_short = self._site_short(site)
        btype = site.site_type if site.site_type != T_UNKNOWN else name.business_type

        if site.verdict == NO and site.site_type == T_DEALER and name.verdict == YES:
            if name.is_known_chain:
                # A dealer group running a Midas / Firestone franchise: the franchise is the business
                return _Decision(
                    YES, 0.6, name.business_type, "Name (known chain; website conflicts)",
                    f"{site_desc} But the business trades as a known repair chain ({name_desc}). Review recommended.",
                    f"known repair chain {name_short}; but the website is a dealership",
                )
            if (
                name.strength >= 0.7
                and not site.franchise_brand
                and site.dealer_score < 4.0
                and site.repair_score >= 2.0
                and any(k in SPECIALIST_CATEGORIES for k in site.repair)
            ):
                # "C&C Auto Repair" whose own site also lists a few used cars: a repair shop that sells cars,
                # not a dealership (franchise dealers and inventory-heavy dealer groups stay No)
                return _Decision(
                    YES, 0.65, name.business_type, "Website + Name (also sells vehicles)",
                    f"Site lists repair services ({', '.join(e.label for e in list(site.repair.values())[:4])}) and some "
                    f"vehicle sales ({site.summary}); the name ({name_desc}) shows a repair shop that also sells "
                    "vehicles. Review recommended.",
                    f"repair shop that also sells vehicles; name {name_short}",
                )
            if name.strength >= 0.7:
                return _Decision(
                    NO, 0.75, T_DEALER_OWNED, "Website + Name",
                    f"{site_desc} This {name_desc} is the dealer's own collision/service operation, which counts as "
                    "a dealership.",
                    f"dealer-owned {name.business_type.lower()} ({site_short})",
                )

        conf = site.strength
        if name.verdict and name.verdict != site.verdict:
            if name.verdict == YES and name.is_known_chain:
                return _Decision(
                    YES, 0.65, name.business_type, "Name (known chain; website conflicts)",
                    f"{site_desc} But the business trades as a known repair chain ({name_desc}). Review recommended.",
                    f"known repair chain {name_short}; but {site_short}",
                )
            conf -= 0.2 if name.strength >= 0.7 else 0.1
            return _Decision(
                site.verdict, conf, btype, "Website (name disagrees)",
                f"{site_desc} The name ({name_desc}) suggests otherwise; the website was trusted.",
                f"{site_short}; name {name_short} disagrees",
            )
        if name.verdict == site.verdict:
            conf = min(0.98, conf + 0.05 + 0.1 * name.strength)
            return _Decision(
                site.verdict, conf, btype, "Website + Name",
                f"{site_desc} Name agrees ({name_desc}).", f"{site_short}; name agrees",
            )
        return _Decision(site.verdict, conf, btype, "Website", site_desc, site_short)

    @staticmethod
    def _registry_note(lead: LeadInfo) -> str:
        if lead.registry:
            return f"defaulted to Yes because the record is registered as '{lead.registry}'."
        return "no registry information available."

    @staticmethod
    def _name_desc(name: NameSignal) -> str:
        if not name.verdict:
            return "no trade signal in name"
        what = "known repair chain" if name.verdict == YES and name.brand else "known non-repair business" if name.brand else name.business_type.lower()
        return f"{name.source} '{', '.join(name.terms[:3])}' -> {what}"

    @staticmethod
    def _name_short(name: NameSignal) -> str:
        if not name.verdict:
            return "gives no signal"
        return f"'{name.terms[0] if name.terms else ''}' -> {name.business_type.lower()}"

    @staticmethod
    def _site_desc(site: SiteProfile) -> str:
        if site.verdict == YES:
            services = [e.label for e in sorted(site.repair.values(), key=lambda e: -e.weight)][:6]
            pages = len({e.url for e in site.repair.values()})
            return f"Site lists {', '.join(services)} ({len(site.repair)} repair service type(s) on {pages} page(s)); {site.summary}."
        if site.site_type == T_DEALER:
            note = " Its service department does not count." if site.repair_score >= 1 else ""
            return f"Site is {_a(site.summary)}.{note}"
        if site.verdict == NO:
            return f"Site is {_a(site.summary)}."
        return f"Site: {site.summary}."

    @staticmethod
    def _site_short(site: SiteProfile) -> str:
        if site.verdict == YES:
            services = [e.label for e in sorted(site.repair.values(), key=lambda e: -e.weight)][:4]
            return f"website lists {', '.join(services)}"
        return f"website is {_a(site.summary)}"

    @staticmethod
    def _why_site_unusable(site: SiteProfile, domain: str, crawl: "CrawlResult | None") -> tuple[str, str]:
        """(sentence for the full reason, fragment for the short reason)."""
        if crawl is None:
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
        return (
            f"Website {domain} has too little content to show its services ({site.word_count} words).",
            "website has too little content",
        )


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
