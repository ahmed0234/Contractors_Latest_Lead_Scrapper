"""Sunbiz name pre-filter for Christmas / holiday lighting installation candidates.

Florida files ~650k corporate records a year, so the engine cannot research every company. This
filter picks registrations worth a website investigation and ranks them. It never qualifies a lead:
every candidate must still be matched to its official website and pass the website classifier.

Tiers (lower = researched first):
1. holiday   - the name itself names the niche ("Red Hat Christmas Lighting", "Freedom Holiday Lights")
2. lighting  - lighting businesses ("Bright Nights Lighting"); many are landscape/electrical and get
               rejected by the website classifier unless they also install holiday lighting
3. adjacent  - only with scope "broad": trades that often sell seasonal light installation as an add-on
               (pressure washing, gutters, window cleaning, landscaping, roofing, electrical). High volume.
"""

import re
from dataclasses import dataclass

TIER_HOLIDAY = 1
TIER_LIGHTING = 2
TIER_ADJACENT = 3

SCOPE_FOCUSED = "focused"  # tiers 1-2
SCOPE_HOLIDAY = "holiday"  # tier 1 only
SCOPE_BROAD = "broad"  # tiers 1-3
SCOPE_MAX_TIER = {SCOPE_HOLIDAY: TIER_HOLIDAY, SCOPE_FOCUSED: TIER_LIGHTING, SCOPE_BROAD: TIER_ADJACENT}

_LIGHT_WORDS = r"(?:lights?|lighting|lites?|illuminations?|illuminating|decor|decorating|decorations?|decorators?|displays?)"

HOLIDAY_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:christmas|xmas|x-mas|yuletide|trimlight)\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:holiday|holidays|seasonal|festive|twinkle|twinkling|jolly|santa'?s|elf|elves|north\s+pole|tinsel|"
        rf"merry|jingle|frosty'?s?|snowflakes?|roofline|permanent|everlights?|celebration)\b"
        rf"(?:\W+\w+){{0,3}}?\W+{_LIGHT_WORDS}\b",
        re.IGNORECASE,
    ),
)

LIGHTING_NAME_PATTERN = re.compile(
    r"\b(?:lights|lighting|lites|illuminations?|illuminating|illuminate|bright\s+nights?|glow)\b", re.IGNORECASE
)

ADJACENT_NAME_PATTERN = re.compile(
    r"\b(?:pressure\s+wash\w*|power\s+wash\w*|soft\s+wash\w*|window\s+clean\w*|gutters?|"
    r"landscap\w*|lawn|roofing|electric|electrical|exterior|outdoor|decor|decorating|holiday)\b",
    re.IGNORECASE,
)

# Registrations that are clearly not on-site installation services
EXCLUDED_NAME_PATTERN = re.compile(
    r"\b(?:films?|movies?|pictures|productions?|studios?|bakery|bakeries|cakes?|cookies|candy|church|ministr\w*|"
    r"foundation|charit\w*|realty|real\s+estate|propert(?:y|ies)|holdings?|apartments?|condominium|condo|"
    r"homeowners|association|trust|capital|invest\w*|mortgage|insurance|dental|medical|clinic|pharmacy|"
    r"restaurant|cafe|grill|tavern|pub|travel|vacations?|cruises?|tours?|inn|hotel|motel|resort|rentals?|"
    r"boutique|apparel|clothing|jewel\w*|salon|spa|nails|beauty|lash\w*|auto|tires?|trucking|logistics|"
    r"freight|christmas\s+trees?|tree\s+farm|ornaments?|gifts?|toys?|music|band|church|school|daycare|"
    r"pageant|festival|parade|theat(?:er|re)|museum|lane|notary|tattoo|aviation|club)\b",
    re.IGNORECASE,
)

NON_PROFIT_FILING_TYPES: tuple[str, ...] = ("DOMNP", "FORNP")


@dataclass(frozen=True)
class NameFilterResult:
    tier: int
    matched: str


def evaluate_company_name(name: str, scope: str = SCOPE_FOCUSED) -> NameFilterResult | None:
    """Return the candidate tier for a registered business name, or None when it is out of scope."""
    if not name:
        return None
    max_tier = SCOPE_MAX_TIER.get(scope, TIER_LIGHTING)
    if EXCLUDED_NAME_PATTERN.search(name):
        return None
    for pattern in HOLIDAY_NAME_PATTERNS:
        m = pattern.search(name)
        if m:
            return NameFilterResult(TIER_HOLIDAY, m.group(0))
    if max_tier >= TIER_LIGHTING:
        m = LIGHTING_NAME_PATTERN.search(name)
        if m:
            return NameFilterResult(TIER_LIGHTING, m.group(0))
    if max_tier >= TIER_ADJACENT:
        m = ADJACENT_NAME_PATTERN.search(name)
        if m:
            return NameFilterResult(TIER_ADJACENT, m.group(0))
    return None


def is_non_profit_filing(filing_type: str) -> bool:
    return (filing_type or "").upper().startswith(NON_PROFIT_FILING_TYPES)
