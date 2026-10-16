"""Website verification engine matching crawled web signals against Sunbiz company identity."""

import re

from contractor_scraper.core.enums import WebsiteStatus
from contractor_scraper.discovery.base import (
    BaseWebsiteVerifier,
    WebsiteCandidate,
    WebsiteVerificationResult,
)
from contractor_scraper.discovery.crawler import WebsiteCrawler
from contractor_scraper.normalization.cleaners import clean_company_name

GENERIC_ENTITY_TOKENS = {
    "inc",
    "llc",
    "corp",
    "corporation",
    "company",
    "co",
    "ltd",
    "services",
    "service",
    "group",
    "enterprises",
    "solutions",
    "contracting",
    "contractor",
    "contractors",
    "florida",
    "fl",
}


class WebsiteVerifier(BaseWebsiteVerifier):
    """Deep verification engine establishing that a candidate domain belongs to the target company.

    Verification signals:
    - Company Name Tokens: Token overlap between Sunbiz name and website title/headers/body.
    - Domain Match: Root domain similarity with unique distinctive company brand name.
    - Geographic Signals: Florida state, city, and zip code mentions on contact/about pages.
    - Trade Category Signals: Core trade service offerings on the website.
    """

    def __init__(self, crawler: WebsiteCrawler | None = None) -> None:
        self.crawler = crawler or WebsiteCrawler()

    async def verify_website(
        self,
        candidate: WebsiteCandidate,
        expected_company_name: str,
        expected_state: str = "FL",
        expected_city: str | None = None,
        category: str = "roofing",
        expected_street_address: str | None = None,
        expected_zip_code: str | None = None,
        expected_phone: str | None = None,
        expected_email: str | None = None,
    ) -> WebsiteVerificationResult:
        """Deep verification of candidate website against entity details."""
        crawl = await self.crawler.crawl_domain(candidate.url)
        if not crawl.is_accessible:
            return WebsiteVerificationResult(
                url=candidate.url,
                domain=candidate.domain,
                status=WebsiteStatus.UNREACHABLE,
                is_verified=False,
                confidence=0.0,
                failure_reason=crawl.error_message or "Website unreachable",
            )

        text_lower = crawl.combined_text.lower()
        title_lower = crawl.homepage.title.lower() if crawl.homepage else ""
        checks: list[str] = []

        # 0. Check for parked domain landers
        parked_signals = [
            "window.location.href='/lander'",
            "buy this domain",
            "domain is for sale",
            "domain may be for sale",
            "this domain is for sale",
            "this domain is parked",
            "hugedomains.com",
            "dan.com",
            "godaddy.com/domainsearch",
            "afternic.com",
            "parkingcrew",
            "sedo.com",
            "domain parked",
            "renew your domain",
            "namecheap.com/domains",
            "parked domain",
        ]
        if any(sig in text_lower for sig in parked_signals):
            return WebsiteVerificationResult(
                url=candidate.url,
                domain=candidate.domain,
                status=WebsiteStatus.PARKED_DOMAIN,
                is_verified=False,
                confidence=0.0,
                failure_reason="Parked or for-sale domain",
            )

        # 1. Company Name Signal (weight: 0.35)
        clean_name = clean_company_name(expected_company_name).lower()
        all_name_tokens = [t for t in re.split(r"[^a-z0-9]", clean_name) if len(t) >= 3]
        distinctive_tokens = [
            t for t in all_name_tokens if t not in GENERIC_ENTITY_TOKENS and t != category.lower()
        ]

        name_score = 0.0
        if clean_name in text_lower or (title_lower and clean_name in title_lower):
            name_score = 1.0
            checks.append("exact_name_match")
        elif all_name_tokens:
            matched_tokens = [
                t
                for t in all_name_tokens
                if (re.search(rf"\b{re.escape(t)}\b", text_lower) or t in title_lower)
            ]
            ratio = len(matched_tokens) / len(all_name_tokens)
            if ratio >= 0.5:
                name_score = ratio
                checks.append(f"token_name_match_{int(ratio * 100)}%")
            else:
                name_score = ratio * 0.5

        # 2. Domain Brand Match Signal (weight: 0.25)
        domain_lower = candidate.domain.lower()
        domain_clean = re.sub(
            r"[^a-z0-9]",
            "",
            domain_lower.replace(".com", "").replace(".net", "").replace(".org", ""),
        )
        domain_brand_match = False
        domain_score = candidate.initial_confidence

        if distinctive_tokens and any(t in domain_clean for t in distinctive_tokens if len(t) >= 4):
            domain_score = max(domain_score, 0.90)
            domain_brand_match = True
            checks.append("distinctive_brand_in_domain")
            if name_score < 0.6:
                name_score = 0.6  # Domain itself proves brand identity

        # 3. Geographic Signal (weight: 0.20)
        geo_score = 0.0
        # Florida mentions or Florida ZIP codes (32xxx, 33xxx, 34xxx)
        has_state = bool(
            re.search(r"\bflorida\b", text_lower)
            or re.search(r"\bfl\b(?:\s+\d{5})?", text_lower)
            or re.search(r"\b3[234]\d{3}\b", text_lower)
        )
        if has_state:
            geo_score += 0.5
            checks.append("state_fl_match")

        # Check City mentions
        if expected_city:
            city_clean = expected_city.strip().lower()
            if city_clean and (
                city_clean in text_lower or (title_lower and city_clean in title_lower)
            ):
                geo_score = min(1.0, geo_score + 0.4)
                checks.append(f"city_{city_clean}_match")

        # Check ZIP code mentions
        if expected_zip_code:
            zip_5 = re.sub(r"\D", "", expected_zip_code)[:5]
            if len(zip_5) == 5 and zip_5 in text_lower:
                geo_score = min(1.0, geo_score + 0.3)
                checks.append(f"zip_{zip_5}_match")

        # Check Street Address mentions
        if expected_street_address:
            addr_tokens = [
                t for t in re.split(r"[^a-z0-9]", expected_street_address.lower()) if len(t) >= 3
            ]
            addr_matched = [
                t for t in addr_tokens if re.search(rf"\b{re.escape(t)}\b", text_lower)
            ]
            if addr_matched and len(addr_matched) >= 2:
                geo_score = min(1.0, geo_score + 0.3)
                checks.append("street_address_match")

        # 4. Category / Trade Signal (weight: 0.20)
        category_score = 0.0
        category_clean = category.strip().lower()
        if category_clean in text_lower or (title_lower and category_clean in title_lower):
            category_score = 1.0
            checks.append(f"category_{category_clean}_match")
        elif category_clean in ("pest control", "pest-control", "pest") and any(
            term in text_lower
            for term in ["pest control", "exterminat", "termite", "bed bug", "rodent", "fumigation"]
        ):
            category_score = 1.0
            checks.append("pest_control_trade_match")
        elif any(
            term in text_lower
            for term in ["contractor", "services", "repairs", "installation", "construction"]
        ):
            category_score = 0.6
            checks.append("general_contractor_terms")

        # 5. Optional Phone Number Verification Signal (High Precision)
        phone_match = False
        if expected_phone:
            raw_phone_digits = re.sub(r"\D", "", expected_phone)
            if len(raw_phone_digits) >= 10:
                phone_10 = raw_phone_digits[-10:]
                c_phones = [
                    re.sub(r"\D", "", p)[-10:] for p in crawl.all_phones if len(re.sub(r"\D", "", p)) >= 10
                ]
                if phone_10 in c_phones or phone_10 in re.sub(r"\D", "", text_lower):
                    phone_match = True
                    checks.append("phone_number_match")

        # 6. Optional Email Domain Match Signal (Custom Domain Only)
        email_dom_match = False
        if expected_email and "@" in expected_email:
            _, email_dom = expected_email.lower().strip().split("@", 1)
            free_email_providers = {
                "gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com",
                "icloud.com", "comcast.net", "att.net", "msn.com", "sbcglobal.net"
            }
            if email_dom not in free_email_providers:
                cand_dom = candidate.domain.lower().strip()
                if cand_dom == email_dom or cand_dom.endswith(f".{email_dom}") or email_dom.endswith(f".{cand_dom}"):
                    email_dom_match = True
                    checks.append("email_domain_match")

        # Composite Confidence Score Calculation
        confidence = (
            (name_score * 0.35)
            + (domain_score * 0.25)
            + (geo_score * 0.20)
            + (category_score * 0.20)
        )

        # Phone match or email domain match significantly elevates confidence
        if phone_match:
            confidence = max(confidence, 0.88)
            name_score = max(name_score, 0.75)
        if email_dom_match:
            confidence = max(confidence, 0.92)
            name_score = max(name_score, 0.85)

        # Pragmatic verification threshold:
        # Verified if composite confidence >= 0.52 and name_score >= 0.35,
        # OR if domain has an exact distinctive brand match with state or category support,
        # OR if phone number / email domain directly matches
        is_verified = (
            (confidence >= 0.52 and name_score >= 0.35)
            or (domain_brand_match and (geo_score > 0 or category_score > 0))
            or phone_match
            or email_dom_match
        )

        status = WebsiteStatus.VERIFIED if is_verified else WebsiteStatus.REJECTED
        failure_reason = None if is_verified else "Insufficient confidence or identity mismatch"

        return WebsiteVerificationResult(
            url=candidate.url,
            domain=candidate.domain,
            status=status,
            is_verified=is_verified,
            confidence=round(confidence, 3),
            verification_checks=checks,
            failure_reason=failure_reason,
        )
