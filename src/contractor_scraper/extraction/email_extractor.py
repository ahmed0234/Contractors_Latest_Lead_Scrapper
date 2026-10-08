"""Business email extraction engine targeting verified company websites."""

from contractor_scraper.discovery.crawler import CrawlResult, WebsiteCrawler
from contractor_scraper.discovery.website_finder import extract_root_domain
from contractor_scraper.extraction.base import BaseEmailExtractor, EmailCandidate

# Blacklisted dummy, template, and system emails
DISPOSABLE_OR_TEMPLATE_DOMAINS: set[str] = {
    "example.com",
    "domain.com",
    "email.com",
    "test.com",
    "mysite.com",
    "yoursite.com",
    "sample.com",
    "wix.com",
    "wixpress.com",
    "wordpress.com",
    "wordpress.org",
    "sentry.io",
    "schema.org",
    "w3.org",
    "cloudflare.com",
    "gravatar.com",
}

DISPOSABLE_LOCAL_PARTS: set[str] = {
    "example",
    "username",
    "yourname",
    "name",
    "user",
    "test",
    "admin",
    "webmaster",
    "hostmaster",
    "postmaster",
    "noreply",
    "no-reply",
    "donotreply",
    "root",
}

PREFERRED_CONTACT_PREFIXES: tuple[str, ...] = (
    "info",
    "contact",
    "office",
    "sales",
    "service",
    "services",
    "support",
    "estimates",
    "estimate",
    "quote",
    "quotes",
    "hello",
    "team",
    "help",
)


class EmailExtractor(BaseEmailExtractor):
    """Extracts, cleans, and scores business email candidates from verified websites."""

    def __init__(self, crawler: WebsiteCrawler | None = None) -> None:
        self.crawler = crawler or WebsiteCrawler()

    def _is_valid_email_candidate(self, email: str) -> bool:
        """Validate structure and reject templates, placeholders, or image filename artifacts."""
        cleaned = email.strip().lower()
        if "@" not in cleaned:
            return False

        local_part, domain = cleaned.split("@", 1)
        if not local_part or not domain:
            return False

        # Reject template domains
        if domain in DISPOSABLE_OR_TEMPLATE_DOMAINS:
            return False

        # Reject generic dummy local parts
        if local_part in DISPOSABLE_LOCAL_PARTS:
            return False

        # Reject image file extensions in local part (e.g. icon.png@domain.com)
        if any(
            local_part.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"]
        ):
            return False

        # Valid domain format
        if "." not in domain or len(domain) < 4:
            return False

        return True

    def _score_email(self, email: str, website_domain: str) -> float:
        """Score email confidence based on domain alignment and contact prefix."""
        cleaned = email.lower().strip()
        local_part, email_domain = cleaned.split("@", 1)
        email_root = extract_root_domain(email_domain)
        web_root = extract_root_domain(website_domain)

        # 1. Official domain match (Gold standard)
        if email_root == web_root:
            if any(local_part.startswith(prefix) for prefix in PREFERRED_CONTACT_PREFIXES):
                return 0.95
            return 0.90

        # 2. Free webmail providers (e.g. Gmail, Yahoo) on verified small contractor site
        if email_root in {
            "gmail.com",
            "yahoo.com",
            "outlook.com",
            "hotmail.com",
            "icloud.com",
            "aol.com",
        }:
            # Acceptable for small contractors if listed on their verified site
            return 0.75

        # 3. Third-party unrelated domain
        return 0.30

    async def extract_emails(
        self,
        website_url: str,
        crawl_result: CrawlResult | None = None,
    ) -> list[EmailCandidate]:
        """Extract candidate business emails from website crawl data."""
        crawl = crawl_result
        if not crawl:
            crawl = await self.crawler.crawl_domain(website_url)

        if not crawl.is_accessible:
            return []

        website_domain = crawl.domain
        candidates: list[EmailCandidate] = []
        seen_emails: set[str] = set()

        for raw_email in crawl.all_emails:
            cleaned = raw_email.lower().strip()
            if not self._is_valid_email_candidate(cleaned):
                continue

            if cleaned in seen_emails:
                continue
            seen_emails.add(cleaned)

            confidence = self._score_email(cleaned, website_domain)
            candidates.append(
                EmailCandidate(
                    email_address=cleaned,
                    source_url=website_url,
                    initial_confidence=confidence,
                )
            )

        # Sort descending by confidence score
        candidates.sort(key=lambda c: c.initial_confidence, reverse=True)
        return candidates
