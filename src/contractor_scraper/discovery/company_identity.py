"""Strict company-to-website identity matching for state-filing (Sunbiz) records.

The generic `WebsiteVerifier` is tuned for recall: a single distinctive token in the domain plus any
geographic or trade word verifies a site. For niche lead lists that is too loose ("Freedom Holiday
Lights LLC" must not be matched to freedomroofing.com), so this module decides identity from the
already-crawled site with stricter, explainable rules:

- Niche words (christmas, holiday, lights, ...) and legal/generic words never identify a company.
- A site is the company's when the full trading name appears on it (plus a corroborating domain or
  location signal), or when the domain carries the company's distinctive brand and the site content
  confirms it. Single-word brands additionally need location or name-coverage corroboration.
- Fully generic names ("Holiday Lighting Services LLC") need the exact name plus a domain or
  city/ZIP match, because many unrelated businesses share such names.
"""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from contractor_scraper.normalization.cleaners import clean_company_name

if TYPE_CHECKING:
    from contractor_scraper.discovery.crawler import CrawlResult

# Words that describe the trade or the legal form rather than identify a particular business
NON_IDENTIFYING_TOKENS: frozenset[str] = frozenset({
    "the", "and", "of", "a", "an", "by", "at", "on", "in", "for", "to",
    "llc", "inc", "corp", "corporation", "co", "company", "pa", "pllc", "ltd", "lp", "llp",
    "group", "enterprise", "enterprises", "solutions", "solution", "services", "service",
    "holdings", "ventures", "industries", "international", "usa", "us",
    "florida", "fl", "south", "north", "central", "southwest", "southeast", "gulf", "coast",
    "christmas", "xmas", "holiday", "holidays", "seasonal", "festive", "season", "seasons",
    "light", "lights", "lighting", "lites", "lite", "illumination", "illuminations",
    "decor", "decorating", "decorations", "decorators", "decorator", "display", "displays",
    "install", "installer", "installers", "installation", "installations", "installing",
    "pro", "pros", "professional", "professionals", "expert", "experts", "specialists",
    "outdoor", "exterior", "permanent", "residential", "commercial", "home", "homes",
    "design", "designs", "electric", "electrical", "landscape", "landscaping", "lawn",
    "cleaning", "pressure", "washing", "wash", "gutter", "gutters", "roofing",
})

FREE_HOSTING_DOMAINS: tuple[str, ...] = (
    "wixsite.com", "squarespace.com", "godaddysites.com", "weebly.com", "wordpress.com",
    "business.site", "square.site", "carrd.co", "webflow.io", "myshopify.com",
)

FLORIDA_PATTERN = re.compile(r"\bflorida\b|\bfl\b\s*\d{5}|,\s*fl\b|\b3[234]\d{3}\b")


def normalize_text(text: str) -> str:
    """Lowercase, '&' -> 'and', strip punctuation, collapse whitespace."""
    text = (text or "").lower().replace("&", " and ").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def name_tokens(company_name: str) -> list[str]:
    return normalize_text(clean_company_name(company_name)).split()


def distinctive_tokens(company_name: str) -> list[str]:
    """Tokens that can identify this specific business (niche/legal words removed)."""
    return [t for t in name_tokens(company_name) if t not in NON_IDENTIFYING_TOKENS and len(t) >= 2]


def domain_slug(domain: str) -> str:
    """Registrable label of a domain without TLD, alphanumerics only ('www.red-hat.co' -> 'redhat')."""
    host = (domain or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    for hosted in FREE_HOSTING_DOMAINS:
        if host.endswith("." + hosted):
            host = host[: -len(hosted) - 1]
            break
    labels = host.split(".")
    label = labels[0] if len(labels) <= 2 else labels[-2] if len(labels[-1]) > 2 else labels[-3]
    return re.sub(r"[^a-z0-9]", "", label)


@dataclass
class IdentityMatch:
    """Outcome of checking whether a crawled website belongs to a specific registered company."""

    is_match: bool
    confidence: float
    reason: str
    checks: list[str] = field(default_factory=list)


def verify_company_identity(
    crawl: "CrawlResult",
    company_name: str,
    city: str = "",
    zip_code: str = "",
    street_address: str = "",
) -> IdentityMatch:
    """Decide whether `crawl` is the official website of `company_name` (a Sunbiz registration)."""
    if not crawl.is_accessible:
        return IdentityMatch(False, 0.0, "Website not accessible", [])

    tokens = name_tokens(company_name)
    if not tokens:
        return IdentityMatch(False, 0.0, "Company name has no usable words", [])
    full_name = " ".join(t for t in tokens if t != "the")
    distinct = distinctive_tokens(company_name)

    homepage = crawl.homepage
    head_text = normalize_text(
        f"{homepage.title} {homepage.meta_description}" if homepage else ""
    )
    page_titles = normalize_text(" ".join(p.title for p in crawl.all_pages))
    body = normalize_text(crawl.combined_text)
    padded_body = f" {body} {head_text} {page_titles} "
    padded_head = f" {head_text} {page_titles} "
    slug = domain_slug(crawl.domain)
    checks: list[str] = []

    # --- Name signals
    exact_in_text = len(full_name) >= 6 and f" {full_name} " in padded_body
    exact_in_head = len(full_name) >= 6 and f" {full_name} " in padded_head
    if exact_in_head:
        checks.append("exact_name_in_title")
    elif exact_in_text:
        checks.append("exact_name_on_site")

    body_words = set(padded_body.split())
    head_words = set(padded_head.split())
    distinct_in_text = bool(distinct) and all(t in body_words for t in distinct)
    distinct_in_head = bool(distinct) and all(t in head_words for t in distinct)
    if distinct_in_head:
        checks.append("brand_words_in_title")
    elif distinct_in_text:
        checks.append("brand_words_on_site")

    # --- Domain signals
    name_slug = "".join(t for t in tokens if t != "the")
    distinct_slug = "".join(distinct)
    # A domain that is a shortened form of the name ("redhatchristmas" for Red Hat Christmas Lighting)
    # must keep the brand and most of the name; "redhat" alone is someone else's site
    domain_full_name = bool(slug) and len(name_slug) >= 5 and (
        name_slug in slug
        or (
            len(slug) >= max(6, 0.6 * len(name_slug))
            and slug in name_slug
            and (not distinct_slug or distinct_slug in slug)
        )
    )
    domain_brand = bool(distinct_slug) and len(distinct_slug) >= 4 and distinct_slug in slug
    if domain_full_name:
        checks.append("full_name_in_domain")
    elif domain_brand:
        checks.append("brand_in_domain")

    # Share of all name words visible as words in page titles or inside the domain (niche words count
    # here: "Freedom Holiday Lights" is not freedommortgage.com even though "freedom" matches)
    # Only the homepage title/description counts: blog or news page titles ("Holiday Toy Drive") do not
    # describe what the business is.
    home_words = set(head_text.split())
    coverage = sum(1 for t in tokens if t in home_words or (len(t) >= 3 and t in slug)) / len(tokens)

    # --- Location signals
    city_norm = normalize_text(city)
    city_match = bool(city_norm) and f" {city_norm} " in padded_body
    zip5 = re.sub(r"\D", "", zip_code or "")[:5]
    zip_match = len(zip5) == 5 and zip5 in padded_body
    street_match = False
    street_norm = normalize_text(street_address)
    street_parts = street_norm.split()
    if len(street_parts) >= 2 and street_parts[0].isdigit():
        street_match = f" {street_parts[0]} {street_parts[1]} " in padded_body
    florida = bool(FLORIDA_PATTERN.search(f"{crawl.combined_text} {head_text}".lower()))
    if city_match:
        checks.append("city_match")
    if zip_match:
        checks.append("zip_match")
    if street_match:
        checks.append("street_address_match")
    if florida:
        checks.append("florida_presence")
    local_match = city_match or zip_match or street_match

    # --- Decision rules
    reason = ""
    is_match = False
    if not distinct:
        # Fully generic name: only an exact, located, or domain-backed match is safe
        if exact_in_text and (domain_full_name or local_match):
            is_match = True
            reason = "Exact generic company name on site with domain or location corroboration"
        else:
            reason = "Generic company name without exact-name plus domain/location corroboration"
    elif exact_in_text and (domain_full_name or domain_brand or local_match or (exact_in_head and florida)):
        is_match = True
        reason = "Exact company name on site with domain/location corroboration"
    elif domain_full_name and (distinct_in_text or exact_in_text):
        is_match = True
        reason = "Domain carries the full company name and the site confirms the brand"
    elif domain_brand and distinct_in_text and (coverage >= 0.6 or (local_match and len(distinct) >= 2)):
        is_match = True
        reason = "Domain carries the distinctive brand and the site confirms it"
    elif distinct_in_head and local_match and (len(distinct) >= 2 or coverage >= 0.6):
        is_match = True
        reason = "Brand in page titles with matching registered city/ZIP/street"
    else:
        reason = "Website does not identify this company (name/domain/location mismatch)"

    score = 0.0
    score += 0.35 if exact_in_head else 0.28 if exact_in_text else 0.18 if distinct_in_head else 0.1 if distinct_in_text else 0.0
    score += 0.3 if domain_full_name else 0.22 if domain_brand else 0.0
    score += 0.15 if local_match else 0.0
    score += 0.05 if florida else 0.0
    score += 0.15 * coverage
    confidence = round(min(0.99, score), 3) if is_match else round(min(0.49, score), 3)
    return IdentityMatch(is_match, confidence, reason, checks)
