"""Production-ready Website Discovery and Pest Control Matching Engine.

Coordinates multi-signal search, DNS and HTTP probing, deep identity verification
(company name, city, state, phone, address, email domain), and pest control service
classification to establish official websites and qualify contractor operations.
"""

from dataclasses import dataclass, field
import logging
import re
from typing import Any
from urllib.parse import urlparse

from contractor_scraper.classification.pest_control_service_classifier import (
    PestControlClassificationResult,
    PestControlServiceClassifier,
)
from contractor_scraper.core.enums import WebsiteStatus
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.base import WebsiteCandidate, WebsiteVerificationResult
from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler
from contractor_scraper.discovery.website_finder import (
    BLOCKED_DIRECTORY_DOMAINS,
    WebsiteFinder,
    extract_root_domain,
)
from contractor_scraper.discovery.website_verifier import WebsiteVerifier

logger = get_logger("company_website_matcher")


@dataclass
class CompanyWebsiteMatchResult:
    """Final result of matching an existing company record to its official website and trade classification."""

    website: str  # Verified URL, e.g. "https://examplepest.com" or ""
    domain: str  # Root domain, e.g. "examplepest.com" or ""
    is_pest_control: bool  # True if verified pest control service contractor
    decision_label: str  # "Yes" or "No"
    confidence: float
    verification_status: str
    reasoning: str
    evidence: list[str] = field(default_factory=list)
    verification_checks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "website": self.website,
            "domain": self.domain,
            "is_pest_control": self.is_pest_control,
            "decision_label": self.decision_label,
            "confidence": self.confidence,
            "verification_status": self.verification_status,
            "reasoning": self.reasoning,
            "evidence": self.evidence,
            "verification_checks": self.verification_checks,
        }


class CompanyWebsiteMatcher:
    """Discovers, verifies, and classifies official websites for company records."""

    def __init__(
        self,
        finder: WebsiteFinder | None = None,
        crawler: WebsiteCrawler | None = None,
        verifier: WebsiteVerifier | None = None,
        classifier: PestControlServiceClassifier | None = None,
    ) -> None:
        self.finder = finder or WebsiteFinder()
        self.crawler = crawler or WebsiteCrawler()
        self.verifier = verifier or WebsiteVerifier(crawler=self.crawler)
        self.classifier = classifier or PestControlServiceClassifier()

    def _normalize_url(self, raw_url: str) -> str:
        """Ensure standard clean https:// URL format."""
        if not raw_url:
            return ""
        url = raw_url.strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        # Normalize trailing slash
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        netloc = parsed.netloc.lower()
        if not netloc:
            return ""
        return f"{parsed.scheme}://{netloc}{path}"

    async def match_company_website(
        self,
        company_name: str,
        city: str,
        state: str,
        phone: str | None = None,
        address: str | None = None,
        zip_code: str | None = None,
        email: str | None = None,
        existing_website: str | None = None,
        category: str = "pest control",
    ) -> CompanyWebsiteMatchResult:
        """Find official website and classify business as an active pest control contractor."""
        clean_name = company_name.strip() if company_name else ""
        if not clean_name:
            return CompanyWebsiteMatchResult(
                website="",
                domain="",
                is_pest_control=False,
                decision_label="No",
                confidence=0.0,
                verification_status="NO_COMPANY_NAME",
                reasoning="Company name is empty or missing",
            )

        clean_city = city.strip() if city else ""
        clean_state = state.strip() if state else "FL"

        # 1. Step 1: Inspect existing website if already populated in the CSV
        if existing_website and existing_website.strip():
            norm_existing = self._normalize_url(existing_website)
            existing_domain = extract_root_domain(norm_existing)
            if existing_domain and not self._is_blocked(existing_domain):
                logger.debug(f"Verifying existing website for {clean_name}: {norm_existing}")
                candidate = WebsiteCandidate(
                    url=norm_existing,
                    domain=existing_domain,
                    discovery_source="existing_csv_field",
                    initial_confidence=0.85,
                )
                ver_res = await self.verifier.verify_website(
                    candidate=candidate,
                    expected_company_name=clean_name,
                    expected_state=clean_state,
                    expected_city=clean_city,
                    category=category,
                    expected_street_address=address,
                    expected_zip_code=zip_code,
                    expected_phone=phone,
                    expected_email=email,
                )
                if ver_res.is_verified:
                    # Crawl pages to evaluate pest control service provision
                    crawl_res = await self.crawler.crawl_domain(candidate.url)
                    class_res = self.classifier.evaluate_crawl(crawl_res, company_name=clean_name)
                    return CompanyWebsiteMatchResult(
                        website=norm_existing,
                        domain=existing_domain,
                        is_pest_control=class_res.is_pest_control,
                        decision_label=class_res.decision_label,
                        confidence=class_res.confidence,
                        verification_status="VERIFIED_EXISTING",
                        reasoning=f"Verified existing website. Classification: {class_res.reasoning}",
                        evidence=class_res.evidence_phrases + class_res.operational_signals,
                        verification_checks=ver_res.verification_checks,
                    )

        # 2. Step 2: Multi-signal public search discovery
        candidates = await self.finder.find_websites(
            company_name=clean_name,
            city=clean_city,
            state=clean_state,
            category=category,
            street_address=address or "",
            zip_code=zip_code or "",
        )

        if not candidates:
            return CompanyWebsiteMatchResult(
                website="",
                domain="",
                is_pest_control=False,
                decision_label="No",
                confidence=0.0,
                verification_status="NO_CANDIDATE_FOUND",
                reasoning="No candidate websites discovered via public search or synthetic probes",
            )

        # 3. Step 3: Deep Identity Verification across candidates
        verified_candidates: list[tuple[WebsiteCandidate, WebsiteVerificationResult]] = []
        for cand in candidates:
            if self._is_blocked(cand.domain):
                continue
            ver = await self.verifier.verify_website(
                candidate=cand,
                expected_company_name=clean_name,
                expected_state=clean_state,
                expected_city=clean_city,
                category=category,
                expected_street_address=address,
                expected_zip_code=zip_code,
                expected_phone=phone,
                expected_email=email,
            )
            if ver.is_verified:
                verified_candidates.append((cand, ver))

        if not verified_candidates:
            # Leave website empty rather than guessing incorrectly
            return CompanyWebsiteMatchResult(
                website="",
                domain="",
                is_pest_control=False,
                decision_label="No",
                confidence=0.0,
                verification_status="UNVERIFIED",
                reasoning=(
                    f"Found {len(candidates)} candidate(s) but none met identity verification "
                    "confidence thresholds."
                ),
            )

        # Sort verified candidates by verification confidence descending
        verified_candidates.sort(key=lambda item: item[1].confidence, reverse=True)
        best_candidate, best_verification = verified_candidates[0]

        # 4. Step 4: Crawl website and classify service provision
        crawl_res = await self.crawler.crawl_domain(best_candidate.url)
        class_res = self.classifier.evaluate_crawl(crawl_res, company_name=clean_name)

        final_url = self._normalize_url(best_candidate.url)
        return CompanyWebsiteMatchResult(
            website=final_url,
            domain=best_candidate.domain,
            is_pest_control=class_res.is_pest_control,
            decision_label=class_res.decision_label,
            confidence=round(
                (best_verification.confidence * 0.4) + (class_res.confidence * 0.6), 3
            ),
            verification_status="VERIFIED",
            reasoning=(
                f"Website verified ({best_candidate.domain}, score {best_verification.confidence}). "
                f"Classification: {class_res.reasoning}"
            ),
            evidence=class_res.evidence_phrases + class_res.operational_signals,
            verification_checks=best_verification.verification_checks,
        )

    def _is_blocked(self, domain: str) -> bool:
        """Check if domain is a directory, review platform, or non-US government/foreign TLD."""
        dom = domain.lower().strip()
        for blocked in BLOCKED_DIRECTORY_DOMAINS:
            if dom == blocked or dom.endswith(f".{blocked}"):
                return True
        for tld in [".pk", ".in", ".cn", ".ru", ".de", ".fr", ".gov", ".edu"]:
            if dom.endswith(tld):
                return True
        return False
