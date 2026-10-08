"""Normalization routines for company names, geographic entities, and addresses."""

import re

from contractor_scraper.core.constants import US_STATES

LEGAL_ENTITY_SUFFIXES_REGEX = re.compile(
    r"(?:,\s*|\s+)(?:"
    r"LIMITED\s+LIABILITY\s+(?:COMPANY|CO\.?)|"
    r"PROFESSIONAL\s+ASSOCIATION|"
    r"L\.?L\.?C\.?|"
    r"P\.?L\.?L\.?C\.?|"
    r"P\.?A\.?|"
    r"P\.?C\.?|"
    r"L\.?L\.?P\.?|"
    r"L\.?P\.?|"
    r"INC(?:ORPORATED|\.)?|"
    r"CORP(?:ORATION|\.)?|"
    r"CO(?:MPANY|\.)?|"
    r"L\.?T\.?D\.?|"
    r"LIMITED|"
    r"CHARTERED"
    r")\.?$",
    re.IGNORECASE,
)

# Inverted mapping from lower-case full state name to 2-letter postal code
_STATE_NAME_TO_CODE = {name.lower(): code for code, name in US_STATES.items()}


ACRONYMS: set[str] = {"HVAC", "AC", "USA", "FL", "US", "LLC", "INC", "CORP", "PA", "PC", "LED"}


def _normalize_casing(text: str) -> str:
    """Normalize ALL-CAPS or all-lowercase names into Title Case while preserving standard acronyms."""
    if not (text.isupper() or text.islower()):
        return text
    words = text.split()
    result = []
    for w in words:
        upper_w = w.upper()
        if upper_w in ACRONYMS:
            result.append(upper_w)
        else:
            result.append(w.capitalize())
    return " ".join(result)


def clean_company_name(raw_name: str) -> str:
    """Standardizes and cleans a company name.

    1. Trims whitespace and removes excessive internal whitespace
    2. Strips corporate legal entity suffixes (LLC, Inc., etc.) for cleaner matching
    3. Normalizes casing (ALL CAPS to Title Case) while preserving trade acronyms
    4. Retains core business identifiers while removing noisy punctuation
    """
    if not raw_name:
        return ""

    cleaned = " ".join(raw_name.strip().split())

    # Iteratively remove legal suffixes (e.g. "Roofing Pros, LLC" -> "Roofing Pros")
    while True:
        match = LEGAL_ENTITY_SUFFIXES_REGEX.search(cleaned)
        if match:
            cleaned = cleaned[: match.start()].strip()
            # Clean any trailing punctuation left after removing suffix
            cleaned = re.sub(r"[,.\s]+$", "", cleaned).strip()
        else:
            break

    cleaned = _normalize_casing(cleaned)
    return cleaned


def clean_state_code(raw_state: str) -> str | None:
    """Normalizes state string into a standard 2-letter uppercase postal code.

    Handles both abbreviation (e.g., 'tx', 'TX') and full state names (e.g., 'Texas').
    """
    if not raw_state:
        return None

    stripped = raw_state.strip().upper()
    if stripped in US_STATES:
        return stripped

    # Try matching full state name
    lower_state = raw_state.strip().lower()
    return _STATE_NAME_TO_CODE.get(lower_state)


def clean_city_name(raw_city: str) -> str | None:
    """Normalizes city name by trimming whitespace and capitalizing words."""
    if not raw_city:
        return None

    cleaned = " ".join(raw_city.strip().split())
    # Clean leading/trailing punctuation
    cleaned = re.sub(r"^[^a-zA-Z]+|[^a-zA-Z]+$", "", cleaned)
    if not cleaned:
        return None

    # Title-case each word in city name
    return cleaned.title()
