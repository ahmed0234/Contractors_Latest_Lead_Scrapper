"""Targeted, polite, and resilient website crawler for business verification and contact extraction."""

import asyncio
import email.utils
import hashlib
import html as html_lib
import json
import random
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import get_logger

logger = get_logger("website_crawler")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

KNOWN_NON_CONTRACTOR_DOMAINS: frozenset[str] = frozenset({
    "homedepot.com",
    "walmart.com",
    "michaels.com",
    "dollartree.com",
    "target.com",
    "homegoods.com",
    "shoprite.com",
    "dollargeneral.com",
    "mavis.com",
    "kohls.com",
    "lot-less.com",
    "costco.com",
    "familydollar.com",
    "amnh.org",
    "wholefoodsmarket.com",
    "centralparknyc.org",
    "pcrichard.com",
    "feverup.com",
    "athome.com",
    "bestbuy.com",
    "tjx.com",
    "tjmaxx.com",
    "truevalue.com",
    "hobbylobby.com",
    "dickblick.com",
    "cb2.com",
    "bbg.org",
    "bryantpark.org",
    "westelm.com",
    "nps.gov",
    "lowes.com",
    "bedbathandbeyond.com",
    "potterybarn.com",
    "crateandbarrel.com",
    "acehardware.com",
    "menards.com",
    "ikea.com",
    "amazon.com",
    "ebay.com",
    "wayfair.com",
    "williams-sonoma.com",
    "overstock.com",
    "aliexpress.com",
    "etsy.com",
    "samsclub.com",
    "bjs.com",
    "macys.com",
    "nordstrom.com",
    "jcpenney.com",
    "sears.com",
    "biglots.com",
    "staples.com",
    "officedepot.com",
    "partycity.com",
    "fivebelow.com",
})

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

BOT_USER_AGENT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# HTTP statuses that indicate a temporary condition worth retrying with backoff
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

# Crawl status categories that represent temporary failures (eligible for a later, more patient retry pass)
TRANSIENT_STATUS_CATEGORIES: frozenset[str] = frozenset(
    {"Timeout", "Connection Error", "Rate Limited", "Server Error"}
)

DNS_FAILURE_MARKERS: tuple[str, ...] = (
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
    "no address associated",
    "temporary failure in name resolution",
    "errno 11001",
    "errno 11004",
    "name resolution",
)

PARKED_DOMAIN_MARKERS: tuple[str, ...] = (
    "this domain is for sale",
    "this domain may be for sale",
    "domain is for sale",
    "buy this domain",
    "the domain name is for sale",
    "domain has expired",
    "this domain has expired",
    "domain parking",
    "parked free",
    "parkingcrew",
    "sedoparking",
    "hugedomains",
    "afternic",
    "dan.com",
    "is available for purchase",
    "future home of something quite cool",
)

JS_SHELL_MARKERS: tuple[str, ...] = (
    'id="root"',
    "id='root'",
    'id="__next"',
    'id="app"',
    "enable javascript",
    "requires javascript",
    "javascript is required",
    "data-reactroot",
    "ng-app",
)

# Block-level elements whose boundaries become " | " segment separators (CrawlerConfig.block_separators)
_BLOCK_TAG_PATTERN = re.compile(
    r"</?(?:p|div|li|ul|ol|nav|header|footer|section|article|aside|main|h[1-6]|br|hr|tr|td|th|table|"
    r"thead|tbody|dt|dd|dl|blockquote|figure|figcaption|form|fieldset|legend|label|option|select|button|a)\b[^>]*>",
    re.IGNORECASE,
)
_SEPARATOR_RUN_PATTERN = re.compile(r"(?:\s*\|\s*){2,}")

# Social / profile platforms whose pages require login to view; only public metadata is read
SOCIAL_PROFILE_DOMAINS: frozenset[str] = frozenset({
    "facebook.com",
    "m.facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "nextdoor.com",
    "linktr.ee",
})

SKIP_LINK_PATTERN = re.compile(
    r"(\.(?:jpe?g|png|gif|webp|svg|pdf|zip|mp4|mov|mp3|docx?|xlsx?|css|js|ico|xml)(?:$|\?))"
    r"|/(?:wp-admin|wp-login|wp-json|wp-content|cart|checkout|my-account|account|login|signin|sign-in|register|feed|tag|author|cdn-cgi|privacy|terms)(?:[/._-]|$)"
    r"|[?&](?:replytocom|share|add-to-cart)=",
    re.IGNORECASE,
)

# Link-scoring vocabularies (matched at word starts against URL path + anchor text)
HOLIDAY_LINK_TERMS: tuple[str, ...] = (
    "christmas", "xmas", "holiday", "festive", "seasonal-light", "seasonal light", "permanent",
    "roofline", "eave", "trimlight", "gemstone", "jellyfish", "everlights", "oelo", "haven",
    "celebright", "light-hanging", "light hanging", "hang", "takedown", "decor",
)
LIGHTING_LINK_TERMS: tuple[str, ...] = ("lighting", "lights", "light", "illumination", "led")
SERVICE_LINK_TERMS: tuple[str, ...] = (
    "services", "service", "what-we-do", "what we do", "residential", "commercial", "winter",
    "seasonal", "gallery", "portfolio", "projects", "our-work", "our work", "faq", "pricing", "packages",
)
INFO_LINK_TERMS: tuple[str, ...] = ("about", "contact", "quote", "estimate", "location", "areas", "blog", "news")

HOLIDAY_PROBE_PATHS: tuple[str, ...] = (
    "/christmas-light-installation",
    "/holiday-lighting",
    "/christmas-lights",
    "/holiday-lights",
    "/permanent-lighting",
    "/services/holiday-lighting",
    "/services/christmas-lights",
    "/services/christmas-light-installation",
    "/christmas-lighting",
    "/permanent-holiday-lighting",
    "/holiday-decorating",
    "/permanent-lights",
    "/trimlight",
)

SOFT_404_MARKERS: tuple[str, ...] = (
    "page not found",
    "404 not found",
    "error 404",
    "404 error",
    "page cannot be found",
    "page you requested could not be found",
    "nothing was found at this location",
    "this page doesn't exist",
    "this page does not exist",
)


def _term_regex(terms: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(t) for t in terms) + ")", re.IGNORECASE)


_HOLIDAY_LINK_RE = _term_regex(HOLIDAY_LINK_TERMS)
_LIGHTING_LINK_RE = _term_regex(LIGHTING_LINK_TERMS)
_SERVICE_LINK_RE = _term_regex(SERVICE_LINK_TERMS)
_INFO_LINK_RE = _term_regex(INFO_LINK_TERMS)
_HOLIDAY_TEXT_RE = re.compile(
    r"\b(christmas|xmas|holiday\s+light|holiday\s+lighting|holiday\s+decor|trimlight|gemstone\s+lights|jellyfish\s+lighting|permanent\s+(?:holiday|christmas|roofline|led|outdoor|exterior)?\s*light)",
    re.IGNORECASE,
)


def score_link(url_path: str, anchor: str = "") -> int:
    """Score an internal link by how likely it is to reveal holiday / decorative lighting services."""
    combined = f"{url_path} {anchor}".lower()
    if _HOLIDAY_LINK_RE.search(combined):
        return 100
    if _LIGHTING_LINK_RE.search(combined):
        return 60
    if _SERVICE_LINK_RE.search(combined):
        return 40
    if _INFO_LINK_RE.search(combined):
        return 20
    return 0


class ArchiveThrottle:
    """Polite, shared rate limiter + circuit breaker for Internet Archive (Wayback Machine) requests."""

    def __init__(self, min_interval: float = 4.0, failure_threshold: int = 3, cooldown_seconds: float = 600.0) -> None:
        self.min_interval = min_interval
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._lock: asyncio.Lock | None = None
        self._last_request = 0.0
        self._consecutive_failures = 0
        self._paused_until = 0.0
        self.requests_made = 0

    async def acquire(self) -> bool:
        """Wait for the next request slot; False while the archive is paused after repeated failures."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            if now < self._paused_until:
                return False
            wait = self._last_request + self.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            self.requests_made += 1
            return True

    def record(self, ok: bool) -> None:
        if ok:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            logger.warning(
                f"Internet Archive unavailable after {self._consecutive_failures} consecutive failures; "
                f"pausing archive lookups for {self.cooldown_seconds:.0f}s"
            )
            self._paused_until = time.monotonic() + self.cooldown_seconds
            self._consecutive_failures = 0


@dataclass
class CrawlerConfig:
    """Independently adjustable timeouts, retries, politeness, and discovery depth for a crawl.

    Defaults reproduce the original lightweight behaviour used by the lead engine and other
    enrichers; niche pipelines can opt into a slower, more thorough profile.
    """

    connect_timeout: float = 6.0
    read_timeout: float = 10.0
    domain_timeout: float | None = None  # None -> legacy min(30, max(15, 2 * timeout_seconds))
    max_retries: int = 1  # additional attempts after the first for transient failures
    backoff_base: float = 1.5
    backoff_max: float = 12.0
    per_domain_concurrency: int = 5
    request_delay: float = 0.0  # pause after each request while holding a per-domain slot
    max_deep_pages: int = 4
    max_probe_pages: int = 4
    respect_robots_txt: bool = False
    use_sitemap: bool = False
    max_sitemap_pages: int = 4
    impersonate_search_bot_on_challenge: bool = True
    render_javascript: bool = False  # requires optional `playwright` package
    # Insert " | " at block-level element boundaries so menu items / list entries / paragraphs stay
    # separate segments in text_content instead of running together into one pseudo-sentence
    block_separators: bool = False
    max_page_bytes: int = 2_097_152
    cache_namespace: str = "crawler"


@dataclass
class FetchOutcome:
    """Result of fetching one URL, including failure diagnostics after retries."""

    url: str
    page: "CrawledPage | None" = None
    status_code: int | None = None
    status_category: str = "OK"
    error: str = ""
    attempts: int = 0

    @property
    def is_transient_failure(self) -> bool:
        return self.page is None and self.status_category in TRANSIENT_STATUS_CATEGORIES


class AnchorExtractor(HTMLParser):
    """HTML parser to safely extract links and their anchor text without regex backtracking."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            # If an unclosed anchor tag was in progress, flush it safely
            if self._current_href:
                anchor_text = " ".join("".join(self._current_text).split()).lower()
                self.links.append((self._current_href, anchor_text))
                self._current_href = None
                self._current_text = []

            for k, v in attrs:
                if k.lower() == "href" and v:
                    val = v.strip()
                    if val and not val.startswith("#") and not val.lower().startswith("javascript:"):
                        self._current_href = val
                        break

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._current_href:
            anchor_text = " ".join("".join(self._current_text).split()).lower()
            self.links.append((self._current_href, anchor_text))
            self._current_href = None
            self._current_text = []

    def error(self, message: str) -> None:
        pass


def decode_cloudflare_email(encoded_hex: str) -> str:
    """Decode Cloudflare XOR-obfuscated email address (data-cfemail or /cdn-cgi/l/email-protection#...)."""
    try:
        if not encoded_hex or len(encoded_hex) < 4:
            return ""
        key = int(encoded_hex[:2], 16)
        chars = []
        for i in range(2, len(encoded_hex), 2):
            chars.append(chr(int(encoded_hex[i : i + 2], 16) ^ key))
        email = "".join(chars).strip()
        if "@" in email and "." in email:
            return email
        return ""
    except Exception:
        return ""


@dataclass
class CrawledPage:
    """Captured content and contact markers from an individual web page."""

    url: str
    status_code: int
    title: str = ""
    meta_description: str = ""
    text_content: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    internal_links: list[str] = field(default_factory=list)
    internal_links_with_anchors: list[tuple[str, str]] = field(default_factory=list)
    is_js_shell: bool = False


@dataclass
class CrawlResult:
    """Aggregated crawl result for a candidate business domain."""

    domain: str
    base_url: str
    is_accessible: bool
    homepage: CrawledPage | None = None
    subpages: list[CrawledPage] = field(default_factory=list)
    all_emails: list[str] = field(default_factory=list)
    all_phones: list[str] = field(default_factory=list)
    combined_text: str = ""
    error_message: str | None = None
    status_code: int | None = None
    status_category: str = "OK"
    # Crawl coverage diagnostics
    pages_attempted: int = 0
    pages_fetched: int = 0
    total_attempts: int = 0
    failed_urls: list[str] = field(default_factory=list)
    robots_blocked_urls: list[str] = field(default_factory=list)
    discovered_relevant_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    incomplete_reason: str = ""
    sitemap_checked: bool = False
    probes_checked: bool = False
    from_cache: bool = False
    archived_snapshot: str = ""  # Wayback Machine timestamp (YYYYMMDDhhmmss) when evaluated from an archive copy

    @property
    def all_pages(self) -> list[CrawledPage]:
        return ([self.homepage] if self.homepage else []) + list(self.subpages)


@dataclass
class _CrawlState:
    """Mutable progress of an in-flight domain crawl (allows partial results on time budget expiry)."""

    homepage: CrawledPage | None = None
    root_url: str = ""
    subpages: list[CrawledPage] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set)
    content_hashes: set[str] = field(default_factory=set)
    pages_attempted: int = 0
    total_attempts: int = 0
    failed_urls: list[str] = field(default_factory=list)
    robots_blocked_urls: list[str] = field(default_factory=list)
    discovered_relevant_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sitemap_checked: bool = False
    probes_checked: bool = False


def _normalize_page_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path}".rstrip("/")


def _content_hash(text: str) -> str:
    return hashlib.sha1(" ".join(text.split())[:20000].encode("utf-8", errors="ignore")).hexdigest()


def _classify_exception(exc: Exception) -> tuple[str, bool]:
    """Map a network exception to (status_category, is_retryable)."""
    msg = str(exc).lower()
    if isinstance(exc, httpx.TimeoutException):
        return "Timeout", True
    if isinstance(exc, httpx.TooManyRedirects):
        return "Redirect Loop", False
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return "Invalid URL", False
    if any(m in msg for m in DNS_FAILURE_MARKERS):
        return "DNS Failure", False
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError, httpx.NetworkError)):
        if "timed out" in msg:
            return "Timeout", True
        return "Connection Error", True
    if "timeout" in msg or "timed out" in msg:
        return "Timeout", True
    return "Connection Error", True


def _parse_retry_after(value: str | None, cap: float) -> float | None:
    if not value:
        return None
    try:
        return min(cap, max(0.0, float(value)))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
        return min(cap, max(0.0, (dt - datetime.now(UTC)).total_seconds()))
    except Exception:
        return None


def page_to_dict(page: CrawledPage, text_limit: int = 15000) -> dict:
    return {
        "url": page.url,
        "status_code": page.status_code,
        "title": page.title,
        "meta_description": page.meta_description,
        "text_content": page.text_content[:text_limit],
        "is_js_shell": page.is_js_shell,
    }


def page_from_dict(data: dict) -> CrawledPage:
    return CrawledPage(
        url=data.get("url", ""),
        status_code=data.get("status_code", 200),
        title=data.get("title", ""),
        meta_description=data.get("meta_description", ""),
        text_content=data.get("text_content", ""),
        is_js_shell=data.get("is_js_shell", False),
    )


class WebsiteCrawler:
    """Responsible, resilient web crawler for extracting contractor verification signals and contact info.

    Features:
    - Multi-stage recovery for HTTP/HTTPS, WWW/non-WWW, and redirect variations
    - Realistic browser headers to bypass automated bot detection
    - Detection of JS interstitials, bot challenges, and Cloudflare protections
    - Prioritized hierarchical subpage discovery (Holiday/Christmas -> Exterior Lighting -> Services -> About/Contact)
    - Concurrent subpage fetching for fast execution
    - Resilient disk caching that retains valid results while re-attempting failed crawls
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout_seconds: float = 12.0,
        max_subpages: int = 6,
        priority_keywords: list[str] | None = None,
        custom_user_agent: str | None = None,
        config: CrawlerConfig | None = None,
    ) -> None:
        self.settings = get_settings()
        self.config = config or CrawlerConfig()
        self.cache_dir = cache_dir or (self.settings.cache_dir / self.config.cache_namespace)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.max_subpages = max_subpages
        self.priority_keywords = priority_keywords
        self.custom_user_agent = custom_user_agent
        self._renderer: _BrowserRenderer | None = None
        self.archive_throttle = ArchiveThrottle()
        if self.config.render_javascript and _BrowserRenderer.available():
            self._renderer = _BrowserRenderer()

    @property
    def domain_timeout(self) -> float:
        if self.config.domain_timeout is not None:
            return self.config.domain_timeout
        return min(30.0, max(15.0, self.timeout_seconds * 2.0))

    def _get_cache_path(self, domain: str) -> Path:
        """Deterministic cache file for a domain crawl."""
        key = hashlib.sha256(domain.lower().encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _generate_candidate_urls(self, raw_url: str) -> list[str]:
        """Generate candidate URL variations (scheme and WWW toggling) for recovery."""
        u = raw_url.strip()
        if "://" not in u:
            u = f"https://{u}"
        parsed = urlparse(u)
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower()
        path = parsed.path or "/"

        candidates: list[str] = []
        # 1. As supplied
        candidates.append(f"{scheme}://{netloc}{path}")
        # 2. Opposite scheme
        alt_scheme = "http" if scheme == "https" else "https"
        candidates.append(f"{alt_scheme}://{netloc}{path}")

        # 3. WWW toggle
        if netloc.startswith("www."):
            non_www = netloc[4:]
            candidates.append(f"{scheme}://{non_www}{path}")
            candidates.append(f"{alt_scheme}://{non_www}{path}")
        else:
            www = f"www.{netloc}"
            candidates.append(f"{scheme}://{www}{path}")
            candidates.append(f"{alt_scheme}://{www}{path}")

        # If a specific subpath was supplied, also add root domain candidates as fallbacks
        if path != "/" and path != "":
            candidates.append(f"{scheme}://{netloc}/")
            candidates.append(f"{alt_scheme}://{netloc}/")
            if netloc.startswith("www."):
                non_www = netloc[4:]
                candidates.append(f"{scheme}://{non_www}/")
                candidates.append(f"{alt_scheme}://{non_www}/")
            else:
                www = f"www.{netloc}"
                candidates.append(f"{scheme}://{www}/")
                candidates.append(f"{alt_scheme}://{www}/")

        # Remove duplicates while preserving priority order
        seen: set[str] = set()
        unique_cands: list[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique_cands.append(c)
        return unique_cands

    def _word_count(self, text: str) -> int:
        """Word count that ignores the segment separators inserted by `block_separators`."""
        words = text.split()
        if self.config.block_separators:
            return sum(1 for w in words if w != "|")
        return len(words)

    def _is_bot_challenge(self, title: str, text: str, html: str = "") -> bool:
        """Detect whether page content is a bot challenge or JavaScript interstitial."""
        combined = f"{title} {text[:2000]} {html[:2500]}".lower()
        patterns = (
            "one moment, please",
            "please wait while your request is being verified",
            "just a moment...",
            "attention required! | cloudflare",
            "access denied",
            "sgcaptcha",
            "sg-captcha",
            ".well-known/sgcaptcha",
            "security service",
            "ddos-guard",
            "checking your browser",
            "cf-browser-verification",
            "challenge-running",
            "turnstile",
            "perimeterx",
        )
        return any(p in combined for p in patterns)

    def _clean_text_from_html(
        self, html: str
    ) -> tuple[str, str, str, list[tuple[str, str]]]:
        """Extract title, meta description, visible body text, footer, and internal links with anchor text."""
        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else ""
        title = html_lib.unescape(title)

        # Extract meta description (with OpenGraph / Twitter fallback)
        meta_match = re.search(
            r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
            html,
            re.IGNORECASE,
        )
        meta_desc = meta_match.group(1).strip() if meta_match else ""
        if not meta_desc:
            og_match = re.search(
                r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']*)["\']',
                html,
                re.IGNORECASE,
            )
            meta_desc = og_match.group(1).strip() if og_match else ""
        meta_desc = html_lib.unescape(meta_desc)

        # Extract footer specifically
        footer_match = re.search(r"<footer[^>]*>(.*?)</footer>", html, re.IGNORECASE | re.DOTALL)
        footer_text = ""
        if footer_match:
            clean_footer = re.sub(
                r"<(script|style|svg)[^>]*>.*?</\1>", " ", footer_match.group(1), flags=re.I | re.S
            )
            if self.config.block_separators:
                clean_footer = _BLOCK_TAG_PATTERN.sub(" | ", clean_footer)
            clean_footer = re.sub(r"<[^>]+>", " ", clean_footer)
            footer_text = " " + " ".join(clean_footer.split())

        # Extract JSON-LD schema markup text (serviceType, name, description, offers, keywords)
        schema_text_parts: list[str] = []
        for sm in re.finditer(
            r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            raw_json = sm.group(1).strip()
            if raw_json:
                try:
                    if self.config.block_separators:
                        # Keep each schema value (e.g. one offered service) its own segment
                        cleaned_json = re.sub(r'["\{\}\[\],]', ' | ', raw_json).replace(":", " ")
                    else:
                        cleaned_json = re.sub(r'["\{\}\[\]:,]', ' ', raw_json)
                    schema_text_parts.append(' '.join(cleaned_json.split()))
                except Exception:
                    pass
        schema_text = (" " + " ".join(schema_text_parts)) if schema_text_parts else ""

        # Clean HTML body safely without regex backtracking
        clean_html = re.sub(r"<script\b[^>]*>[\s\S]*?<\/script>", " ", html, flags=re.IGNORECASE)
        clean_html = re.sub(r"<style\b[^>]*>[\s\S]*?<\/style>", " ", clean_html, flags=re.IGNORECASE)
        clean_html = re.sub(r"<svg\b[^>]*>[\s\S]*?<\/svg>", " ", clean_html, flags=re.IGNORECASE)
        clean_html = re.sub(r"<noscript\b[^>]*>[\s\S]*?<\/noscript>", " ", clean_html, flags=re.IGNORECASE)
        if self.config.block_separators:
            clean_html = _BLOCK_TAG_PATTERN.sub(" | ", clean_html)
        text = re.sub(r"<[^>]+>", " ", clean_html)
        text = html_lib.unescape(text)
        body_text = " ".join(text.split()) + footer_text + schema_text
        if self.config.block_separators:
            body_text = _SEPARATOR_RUN_PATTERN.sub(" | ", body_text).strip(" |")

        # Extract links
        links_list: list[str] = []
        for m in re.finditer(
            r'<a\s+[^>]*href=["\']([^"\'#\s>]+)["\'][^>]*>',
            html,
            re.IGNORECASE,
        ):
            href = m.group(1).strip()
            if href and not href.startswith("#") and href not in links_list:
                links_list.append(href)

        all_hrefs = re.findall(r'href=["\']([^"\'#\s>]+)["\']', html, re.IGNORECASE)
        for h in all_hrefs:
            clean_h = h.strip()
            if clean_h and not clean_h.startswith("#") and clean_h not in links_list:
                links_list.append(clean_h)

        return title, meta_desc, body_text, links_list

    def _extract_links_with_anchors(self, html: str) -> list[tuple[str, str]]:
        """Extract links accompanied by their visible anchor text for priority matching."""
        if not html:
            return []
        try:
            parser = AnchorExtractor()
            parser.feed(html)
            return parser.links
        except Exception:
            hrefs = re.findall(r'href=["\']([^"\'#\s>]+)["\']', html, re.IGNORECASE)
            return [(h.strip(), "") for h in hrefs if h.strip() and not h.startswith("#")]

    def _extract_emails_and_phones(self, html: str, text: str) -> tuple[list[str], list[str]]:
        """Extract potential email addresses and phone numbers from HTML, footers, JSON-LD, and text."""
        raw_candidates: list[str] = []
        unescaped_html = html_lib.unescape(html)

        # 1. Cloudflare protected emails (data-cfemail="...")
        cf_matches = re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', html)
        for cf in cf_matches:
            dec = decode_cloudflare_email(cf)
            if dec:
                raw_candidates.append(dec)

        # 1b. Cloudflare protected link hrefs (/cdn-cgi/l/email-protection#...)
        cf_links = re.findall(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)", html)
        for cf in cf_links:
            dec = decode_cloudflare_email(cf)
            if dec:
                raw_candidates.append(dec)

        # 2. mailto: links
        mailto_matches = re.findall(r"mailto:\s*([^\"'#\s>]+)", unescaped_html, re.IGNORECASE)
        for m in mailto_matches:
            clean_m = urllib.parse.unquote(m).split("?")[0].strip()
            for sub_e in re.split(r"[,;]", clean_m):
                sub_e = sub_e.strip()
                if EMAIL_REGEX.match(sub_e):
                    raw_candidates.append(sub_e)

        # 3. JSON-LD schema markup
        schema_emails = re.findall(
            r'["\'](?:email|mail)["\']\s*:\s*["\']([^"\']+)["\']', html, re.IGNORECASE
        )
        for se in schema_emails:
            clean_se = se.replace("mailto:", "").strip()
            if EMAIL_REGEX.match(clean_se):
                raw_candidates.append(clean_se)

        # 4. Direct regex
        raw_candidates.extend(EMAIL_REGEX.findall(text))
        raw_candidates.extend(EMAIL_REGEX.findall(unescaped_html))

        # 5. Obfuscated
        obfuscated = re.findall(
            r"([a-zA-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\[@\])\s*([a-zA-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\.)\s*([a-zA-Z]{2,})",
            text,
            re.IGNORECASE,
        )
        for user, dom, tld in obfuscated:
            raw_candidates.append(f"{user}@{dom}.{tld}")

        dummy_domains = {
            "example.com",
            "domain.com",
            "yourdomain.com",
            "yourcompany.com",
            "email.com",
            "sentry.io",
            "wixpress.com",
            "wordpress.org",
            "cloudflare.com",
            "schema.org",
            "gravatar.com",
            "mysite.com",
        }

        valid_emails = []
        for e in raw_candidates:
            lower_e = e.lower().strip()
            if "@" not in lower_e:
                continue
            local_part, domain_part = lower_e.split("@", 1)
            if any(
                local_part.endswith(ext) or domain_part.endswith(ext)
                for ext in [
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".svg",
                    ".gif",
                    ".webp",
                    ".bmp",
                    ".css",
                    ".js",
                    ".woff",
                    ".woff2",
                    ".ttf",
                ]
            ):
                continue
            if "." not in domain_part or len(domain_part) < 4:
                continue
            if domain_part in dummy_domains or any(d in domain_part for d in dummy_domains):
                continue
            valid_emails.append(lower_e)

        # Phones
        tel_matches = re.findall(r"tel:\s*([^\"'#\s>]+)", unescaped_html, re.IGNORECASE)
        schema_phones = re.findall(
            r'["\'](?:telephone|phone)["\']\s*:\s*["\']([^"\']+)["\']', html, re.IGNORECASE
        )
        clean_tels = [
            re.sub(r"[^\d+]", "", urllib.parse.unquote(t))
            for t in tel_matches + schema_phones
            if len(re.sub(r"\D", "", t)) >= 10
        ]
        text_phones = PHONE_REGEX.findall(text)
        all_phones = list(dict.fromkeys(clean_tels + text_phones))

        return list(dict.fromkeys(valid_emails)), all_phones

    def _rank_links(
        self,
        root_url: str,
        links: list[tuple[str, str]] | list[str],
        exclude: set[str] | None = None,
    ) -> list[tuple[int, str]]:
        """Return same-domain internal links scored by relevance, best first (deduplicated)."""
        base_netloc = urlparse(root_url).netloc.lower().replace("www.", "")
        seen = {_normalize_page_url(root_url)} | (exclude or set())
        scored: list[tuple[int, int, str]] = []

        for order, item in enumerate(links):
            href, anchor = item if isinstance(item, tuple) else (str(item), "")
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "sms:", "data:")):
                continue
            full_url = urljoin(root_url, href)
            target = urlparse(full_url)
            if target.scheme not in ("http", "https"):
                continue
            if target.netloc.lower().replace("www.", "") != base_netloc:
                continue
            if SKIP_LINK_PATTERN.search(full_url):
                continue

            # Strip query/fragment
            clean_url = f"{target.scheme}://{target.netloc}{target.path}".rstrip("/")
            norm = _normalize_page_url(clean_url)
            if not clean_url or norm in seen:
                continue
            score = score_link(target.path.lower(), (anchor or "").lower())
            if score <= 0:
                continue
            seen.add(norm)
            scored.append((score, order, clean_url))

        scored.sort(key=lambda t: (-t[0], t[1]))
        return [(score, url) for score, _, url in scored]

    def _select_priority_subpages(
        self,
        root_url: str,
        links: list[tuple[str, str]] | list[str],
        max_count: int,
        exclude: set[str] | None = None,
        include_fallbacks: bool = True,
    ) -> list[str]:
        """Select the most relevant internal pages (Holiday -> Lighting -> Services -> About/Contact)."""
        ranked = self._rank_links(root_url, links, exclude)
        selected = [url for _, url in ranked[:max_count]]
        seen = {_normalize_page_url(u) for u in selected} | (exclude or set())
        seen.add(_normalize_page_url(root_url))

        # Fallback well-known paths if fewer found
        if include_fallbacks and len(selected) < max_count:
            common_fallbacks = [
                "/christmas-light-installation",
                "/holiday-lighting",
                "/christmas-lights",
                "/services/holiday-lighting",
                "/services",
                "/about",
                "/contact",
            ]
            for fb in common_fallbacks:
                fb_url = urljoin(root_url, fb).rstrip("/")
                if _normalize_page_url(fb_url) not in seen and len(selected) < max_count:
                    selected.append(fb_url)
                    seen.add(_normalize_page_url(fb_url))

        return selected

    def _request_timeout(self) -> httpx.Timeout:
        cfg = self.config
        return httpx.Timeout(
            connect=cfg.connect_timeout,
            read=cfg.read_timeout,
            write=10.0,
            pool=cfg.connect_timeout + cfg.read_timeout,
        )

    async def _request(
        self, client: httpx.AsyncClient, url: str, timeout_cfg: httpx.Timeout
    ) -> tuple[int, httpx.Headers, str | None]:
        """Perform one GET, streaming at most `max_page_bytes`. Returns (status, headers, html|None)."""
        async with client.stream("GET", url, timeout=timeout_cfg) as resp:
            status = resp.status_code
            if status >= 400 and status != 403:
                return status, resp.headers, ""

            content_type = resp.headers.get("content-type", "").lower()
            if content_type and not any(ct in content_type for ct in ["text", "html", "xml", "json"]):
                return status, resp.headers, None

            chunks: list[bytes] = []
            total_bytes = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes >= self.config.max_page_bytes:
                    break

            raw_bytes = b"".join(chunks)
            encoding = resp.encoding or "utf-8"
            try:
                html = raw_bytes.decode(encoding, errors="replace")
            except Exception:
                html = raw_bytes.decode("utf-8", errors="replace")
            return status, resp.headers, html

    def _build_page(self, url: str, status_code: int, headers: httpx.Headers, html: str) -> CrawledPage:
        title, meta_desc, body_text, clean_links = self._clean_text_from_html(html)
        links_with_anchors = self._extract_links_with_anchors(html)
        emails, phones = self._extract_emails_and_phones(html, body_text)

        # Detect anti-bot challenge signatures in headers or meta refresh (e.g. SiteGround sgcaptcha, Cloudflare turnstile)
        html_lower = html[:60000].lower()
        is_challenge_header = "sg-captcha" in headers or "cf-mitigated" in headers
        is_meta_challenge = status_code == 202 and ("sgcaptcha" in html_lower or '<meta http-equiv="refresh"' in html_lower)
        if is_challenge_header or is_meta_challenge:
            title = title or "Bot Verification Required"
            body_text = f"sgcaptcha bot challenge required {body_text}"

        word_count = self._word_count(body_text)
        is_js_shell = word_count < 60 and (
            any(m in html_lower for m in JS_SHELL_MARKERS) or html_lower.count("<script") >= 8
        )

        return CrawledPage(
            url=url,
            status_code=status_code,
            title=title,
            meta_description=meta_desc,
            text_content=body_text[:35000],
            emails=emails,
            phones=phones,
            internal_links=clean_links,
            internal_links_with_anchors=links_with_anchors,
            is_js_shell=is_js_shell,
        )

    async def _fetch_outcome(
        self,
        client: httpx.AsyncClient,
        url: str,
        gate: asyncio.Semaphore | None = None,
        max_attempts: int | None = None,
    ) -> FetchOutcome:
        """Fetch a page with exponential-backoff retries for transient failures (timeouts, 429, 5xx)."""
        cfg = self.config
        outcome = FetchOutcome(url=url)
        timeout_cfg = self._request_timeout()
        attempts_allowed = (cfg.max_retries + 1) if max_attempts is None else max(1, max_attempts)

        for attempt in range(attempts_allowed):
            outcome.attempts = attempt + 1
            retry_after: float | None = None
            retryable = False
            try:
                if gate is not None:
                    async with gate:
                        status, headers, html = await self._request(client, url, timeout_cfg)
                        if cfg.request_delay > 0:
                            await asyncio.sleep(cfg.request_delay)
                else:
                    status, headers, html = await self._request(client, url, timeout_cfg)
            except Exception as exc:
                category, retryable = _classify_exception(exc)
                # Allow a single retry for resolver hiccups before declaring DNS failure
                if category == "DNS Failure" and attempt == 0:
                    retryable = True
                outcome.status_category = category
                outcome.error = (f"{type(exc).__name__}: {exc}".strip(": "))[:300]
                logger.debug(f"Fetch attempt {attempt + 1} failed for {url}: {outcome.error}")
            else:
                outcome.status_code = status
                if status in RETRYABLE_STATUS_CODES:
                    outcome.status_category = "Rate Limited" if status == 429 else "Server Error"
                    outcome.error = f"HTTP {status}"
                    retry_after = _parse_retry_after(headers.get("retry-after"), cfg.backoff_max)
                    retryable = True
                elif status >= 400 and status != 403:
                    outcome.status_category = f"HTTP Error {status}"
                    outcome.error = f"HTTP {status}"
                    return outcome
                elif html is None:
                    outcome.status_category = "Non-HTML Content"
                    outcome.error = "Non-HTML content"
                    return outcome
                else:
                    outcome.page = self._build_page(url, status, headers, html)
                    outcome.status_category = "OK"
                    outcome.error = ""
                    return outcome

            if not retryable or attempt >= attempts_allowed - 1:
                break
            delay = retry_after if retry_after is not None else min(cfg.backoff_max, cfg.backoff_base * (2**attempt))
            await asyncio.sleep(delay + random.uniform(0.0, 0.4))

        return outcome

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> CrawledPage | None:
        """Fetch and parse an individual web page (None on failure)."""
        return (await self._fetch_outcome(client, url)).page

    async def _fetch_text(
        self, client: httpx.AsyncClient, url: str, gate: asyncio.Semaphore | None = None
    ) -> str | None:
        """Single-attempt fetch of a small text resource (robots.txt, sitemap XML)."""
        timeout_cfg = httpx.Timeout(
            connect=min(8.0, self.config.connect_timeout),
            read=min(12.0, self.config.read_timeout),
            write=5.0,
            pool=15.0,
        )
        try:
            if gate is not None:
                async with gate:
                    status, _, text = await self._request(client, url, timeout_cfg)
            else:
                status, _, text = await self._request(client, url, timeout_cfg)
        except Exception as exc:
            logger.debug(f"Text fetch failed for {url}: {exc}")
            return None
        if status != 200 or not text:
            return None
        return text

    async def _load_robots(
        self, client: httpx.AsyncClient, root_url: str, gate: asyncio.Semaphore
    ) -> urllib.robotparser.RobotFileParser | None:
        text = await self._fetch_text(client, urljoin(root_url, "/robots.txt"), gate)
        if not text or "<html" in text[:500].lower():
            return None
        parser = urllib.robotparser.RobotFileParser()
        try:
            parser.parse(text.splitlines())
        except Exception:
            return None
        return parser

    @staticmethod
    def _robots_allowed(
        parser: urllib.robotparser.RobotFileParser | None, user_agent: str, url: str
    ) -> bool:
        if parser is None:
            return True
        try:
            return parser.can_fetch(user_agent, url)
        except Exception:
            return True

    async def _discover_sitemap_urls(
        self,
        client: httpx.AsyncClient,
        root_url: str,
        robots: urllib.robotparser.RobotFileParser | None,
        gate: asyncio.Semaphore,
    ) -> list[str]:
        """Collect page URLs from robots-declared or conventional sitemap locations."""
        declared: list[str] = []
        if robots is not None:
            try:
                declared = list(robots.site_maps() or [])
            except Exception:
                declared = []
        to_visit = declared[:3] or [
            urljoin(root_url, "/sitemap.xml"),
            urljoin(root_url, "/sitemap_index.xml"),
            urljoin(root_url, "/wp-sitemap.xml"),
        ]
        visited: set[str] = set()
        locs: list[str] = []
        fetches = 0
        while to_visit and fetches < 7 and len(locs) < 3000:
            sm_url = to_visit.pop(0)
            if sm_url in visited:
                continue
            visited.add(sm_url)
            fetches += 1
            text = await self._fetch_text(client, sm_url, gate)
            if not text or "<loc" not in text.lower():
                continue
            entries = [
                html_lib.unescape(m.strip())
                for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.IGNORECASE | re.DOTALL)
            ]
            if "<sitemapindex" in text[:2000].lower():
                # Page/service/post sitemaps first; product/image/tag sitemaps last
                entries.sort(
                    key=lambda u: (
                        0 if re.search(r"page|service|location|post", u, re.I) else
                        2 if re.search(r"product|image|tag|author|categor", u, re.I) else 1
                    )
                )
                to_visit = entries[:4] + to_visit
            else:
                locs.extend(entries)
                if not declared:
                    break
        return locs

    def _accept_page(self, page: CrawledPage, state: _CrawlState) -> bool:
        """Reject soft-404s, duplicate content (redirects to homepage), and bot challenges."""
        head = f"{page.title} {page.text_content[:500]}".lower()
        if any(m in head for m in SOFT_404_MARKERS):
            return False
        if self._word_count(page.text_content) < 5:
            return False
        if self._is_bot_challenge(page.title, page.text_content):
            return False
        digest = _content_hash(page.text_content)
        if digest in state.content_hashes:
            return False
        state.content_hashes.add(digest)
        return True

    async def _fetch_batch(
        self,
        client: httpx.AsyncClient,
        urls: list[str],
        gate: asyncio.Semaphore,
        state: _CrawlState,
        robots: urllib.robotparser.RobotFileParser | None,
        user_agent: str,
    ) -> list[CrawledPage]:
        """Fetch a batch of URLs concurrently (bounded per domain), skipping visited and robots-disallowed URLs."""
        to_fetch: list[str] = []
        for u in urls:
            norm = _normalize_page_url(u)
            if norm in state.seen_urls:
                continue
            state.seen_urls.add(norm)
            if not self._robots_allowed(robots, user_agent, u):
                state.robots_blocked_urls.append(u)
                continue
            to_fetch.append(u)

        if not to_fetch:
            return []

        outcomes = await asyncio.gather(
            *[self._fetch_outcome(client, u, gate) for u in to_fetch], return_exceptions=True
        )
        accepted: list[CrawledPage] = []
        for u, outcome in zip(to_fetch, outcomes, strict=True):
            state.pages_attempted += 1
            if isinstance(outcome, BaseException):
                state.failed_urls.append(f"{u} (Connection Error)")
                continue
            state.total_attempts += outcome.attempts
            if outcome.page is not None:
                if self._accept_page(outcome.page, state):
                    state.subpages.append(outcome.page)
                    accepted.append(outcome.page)
            elif outcome.is_transient_failure:
                state.failed_urls.append(f"{u} ({outcome.status_category})")
        return accepted

    def _has_holiday_signal(self, state: _CrawlState) -> bool:
        pages = ([state.homepage] if state.homepage else []) + state.subpages
        return any(_HOLIDAY_TEXT_RE.search(f"{p.title} {p.meta_description} {p.text_content}") for p in pages)

    @staticmethod
    def _summarize_failures(outcomes: list[FetchOutcome]) -> tuple[str, str, int | None]:
        """Pick the most informative failure category across homepage candidate attempts."""
        if not outcomes:
            return "Connection Error", "Connection failed", None
        categories = [o.status_category for o in outcomes]
        if all(c == "DNS Failure" for c in categories):
            category = "DNS Failure"
        elif all(c == "Blocked by robots.txt" for c in categories):
            category = "Blocked by robots.txt"
        else:
            category = next(c for c in categories if c not in ("DNS Failure", "Blocked by robots.txt"))
        details: list[str] = []
        for o in outcomes:
            line = f"{o.url} -> {o.status_category}" + (f" ({o.error})" if o.error and o.error not in o.status_category else "")
            if o.attempts > 1:
                line += f" after {o.attempts} attempts"
            if line not in details:
                details.append(line)
        status_code = next((o.status_code for o in outcomes if o.status_code), None)
        return category, "; ".join(details[:4]), status_code

    async def crawl_domain(self, base_url: str, extra_seed_urls: list[str] | None = None) -> CrawlResult:
        """Crawl homepage and relevant subpages with recovery fallbacks, retries, and an overall time budget."""
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        domain = parsed.netloc.lower().replace("www.", "")
        root_url = f"{parsed.scheme or 'https'}://{parsed.netloc}"

        # Fast path: immediately identify major national retail chains and public venues
        for ncd in KNOWN_NON_CONTRACTOR_DOMAINS:
            if ncd in domain or domain.endswith(ncd):
                return CrawlResult(
                    domain=domain,
                    base_url=root_url,
                    is_accessible=True,
                    combined_text=(
                        f"National retail department store chain / public venue / e-commerce seller ({ncd}). "
                        "Sells retail goods and merchandise; does not provide contractor on-site Christmas or holiday light installation services."
                    ),
                    status_code=200,
                    status_category="OK",
                )

        # 1. Check disk cache (only complete, accessible crawls are reused)
        cache_key = domain if not parsed.path.strip("/") or domain not in SOCIAL_PROFILE_DOMAINS else f"{domain}{parsed.path}"
        cache_path = self._get_cache_path(cache_key)
        cached = self._load_cache(cache_path, root_url)
        if cached is not None:
            return cached

        # 2. Perform live resilient crawl
        state = _CrawlState()
        crawl_timeout = self.domain_timeout
        try:
            return await asyncio.wait_for(
                self._execute_domain_crawl(domain, base_url, cache_path, state, extra_seed_urls),
                timeout=crawl_timeout,
            )
        except TimeoutError:
            if state.homepage is not None:
                logger.debug(f"Domain crawl for {domain} hit {crawl_timeout}s budget; returning partial result.")
                return self._build_result(
                    domain,
                    state,
                    incomplete_reason=f"Crawl time budget ({crawl_timeout:.0f}s) reached before all relevant pages were evaluated",
                )
            logger.debug(f"Domain crawl timed out for {domain} after {crawl_timeout}s.")
            return CrawlResult(
                domain=domain,
                base_url=root_url,
                is_accessible=False,
                error_message=f"Domain crawl timed out after {crawl_timeout:.0f}s across all connection attempts",
                status_category="Timeout",
                total_attempts=state.total_attempts,
            )
        except Exception as exc:
            logger.debug(f"Domain crawl unexpected error for {domain}: {exc}")
            err_cat = "Timeout" if "timeout" in str(exc).lower() else "Connection Error"
            return CrawlResult(
                domain=domain,
                base_url=root_url,
                is_accessible=False,
                error_message=str(exc),
                status_category=err_cat,
                total_attempts=state.total_attempts,
            )

    def _build_result(self, domain: str, state: _CrawlState, incomplete_reason: str = "") -> CrawlResult:
        pages = ([state.homepage] if state.homepage else []) + state.subpages
        emails: list[str] = []
        phones: list[str] = []
        texts: list[str] = []
        for p in pages:
            texts.append(f"{p.title} {p.meta_description} {p.text_content}")
            emails.extend(p.emails)
            phones.extend(p.phones)

        if not incomplete_reason and state.failed_urls and not state.subpages:
            incomplete_reason = (
                f"Homepage loaded but {len(state.failed_urls)} of {state.pages_attempted} subpage requests failed"
            )

        return CrawlResult(
            domain=domain,
            base_url=state.root_url,
            is_accessible=True,
            homepage=state.homepage,
            subpages=list(state.subpages),
            all_emails=list(dict.fromkeys(emails)),
            all_phones=list(dict.fromkeys(phones)),
            combined_text=" ".join(texts)[:120000],
            status_code=state.homepage.status_code if state.homepage else 200,
            status_category="OK",
            pages_attempted=state.pages_attempted + 1,
            pages_fetched=len(pages),
            total_attempts=state.total_attempts,
            failed_urls=list(state.failed_urls),
            robots_blocked_urls=list(state.robots_blocked_urls),
            discovered_relevant_urls=list(state.discovered_relevant_urls),
            notes=list(state.notes),
            incomplete_reason=incomplete_reason,
            sitemap_checked=state.sitemap_checked,
            probes_checked=state.probes_checked,
        )

    async def _execute_domain_crawl(
        self,
        domain: str,
        base_url: str,
        cache_path: Path,
        state: _CrawlState | None = None,
        extra_seed_urls: list[str] | None = None,
    ) -> CrawlResult:
        """Execute network crawl: homepage recovery, robots, link/sitemap discovery, deep pages, and probes."""
        cfg = self.config
        state = state if state is not None else _CrawlState()

        headers = dict(DEFAULT_BROWSER_HEADERS)
        if self.custom_user_agent:
            headers["User-Agent"] = self.custom_user_agent
        user_agent = headers["User-Agent"]

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        gate = asyncio.Semaphore(max(1, cfg.per_domain_concurrency))
        is_social = domain in SOCIAL_PROFILE_DOMAINS

        # Generate recovery candidates (https/http, www/non-www)
        candidates = self._generate_candidate_urls(base_url)

        async with httpx.AsyncClient(
            timeout=self._request_timeout(),
            follow_redirects=True,
            max_redirects=8,
            verify=False,  # Tolerant to self-signed or expired contractor certificates
            headers=headers,
            limits=limits,
        ) as client:
            homepage: CrawledPage | None = None
            successful_root: str = base_url
            failures: list[FetchOutcome] = []
            dead_hosts: set[str] = set()
            robots_by_host: dict[str, urllib.robotparser.RobotFileParser | None] = {}

            # Attempt each candidate URL (full retries on the primary candidate, fewer on alternates)
            for i, cand_url in enumerate(candidates):
                host = urlparse(cand_url).netloc.lower()
                if host in dead_hosts:
                    continue
                if cfg.respect_robots_txt:
                    if host not in robots_by_host:
                        robots_by_host[host] = await self._load_robots(client, cand_url, gate)
                    if not self._robots_allowed(robots_by_host[host], user_agent, cand_url):
                        failures.append(
                            FetchOutcome(url=cand_url, status_category="Blocked by robots.txt", error="Disallowed by robots.txt")
                        )
                        continue
                outcome = await self._fetch_outcome(
                    client, cand_url, gate, max_attempts=None if i == 0 else min(2, cfg.max_retries + 1)
                )
                state.total_attempts += outcome.attempts
                if outcome.page is not None:
                    homepage = outcome.page
                    successful_root = cand_url
                    break
                failures.append(outcome)
                if outcome.status_category == "DNS Failure":
                    dead_hosts.add(host)

            if homepage is None:
                category, error_text, status_code = self._summarize_failures(failures)
                result = CrawlResult(
                    domain=domain,
                    base_url=base_url,
                    is_accessible=False,
                    error_message=error_text,
                    status_code=status_code,
                    status_category=category,
                    total_attempts=state.total_attempts,
                )
                self._save_cache(cache_path, result)
                return result

            # Check for Bot Challenge / JavaScript Interstitial
            if (
                cfg.impersonate_search_bot_on_challenge
                and (self._is_bot_challenge(homepage.title, homepage.text_content) or homepage.status_code == 403)
            ):
                # Attempt fallback with Bot User-Agent (often permitted by firewalls for indexing)
                try:
                    bot_resp = await client.get(
                        successful_root,
                        headers={"User-Agent": BOT_USER_AGENT, "Accept": "*/*"},
                        timeout=httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0),
                    )
                    if bot_resp.status_code in (200, 201, 202):
                        b_title, b_meta, b_body, b_links = self._clean_text_from_html(bot_resp.text)
                        if not self._is_bot_challenge(b_title, b_body) and len(b_body.split()) >= 30:
                            b_emails, b_phones = self._extract_emails_and_phones(bot_resp.text, b_body)
                            homepage = CrawledPage(
                                url=successful_root,
                                status_code=bot_resp.status_code,
                                title=b_title,
                                meta_description=b_meta,
                                text_content=b_body[:18000],
                                emails=b_emails,
                                phones=b_phones,
                                internal_links=b_links,
                            )
                except Exception:
                    pass

            # Re-evaluate if still bot challenged
            if self._is_bot_challenge(homepage.title, homepage.text_content) or homepage.status_code == 403:
                # If meta tags contain genuine contractor keywords, capture them
                has_substantive_meta = any(
                    k in f"{homepage.title} {homepage.meta_description}".lower()
                    for k in ["christmas", "holiday", "lighting", "light", "contractor", "services"]
                )
                if not has_substantive_meta:
                    result = CrawlResult(
                        domain=domain,
                        base_url=successful_root,
                        is_accessible=False,
                        homepage=homepage,
                        error_message="Website protected by web firewall / bot challenge (not bypassed)",
                        status_code=homepage.status_code,
                        status_category="Bot Challenge",
                        total_attempts=state.total_attempts,
                    )
                    self._save_cache(cache_path, result)
                    return result

            # Parked / for-sale domains carry no business content to evaluate
            head_lower = f"{homepage.title} {homepage.text_content[:3000]}".lower()
            if self._word_count(homepage.text_content) < 400 and any(m in head_lower for m in PARKED_DOMAIN_MARKERS):
                result = CrawlResult(
                    domain=domain,
                    base_url=successful_root,
                    is_accessible=False,
                    homepage=homepage,
                    error_message="Domain is parked, expired, or listed for sale (no business website content)",
                    status_code=homepage.status_code,
                    status_category="Parked Domain",
                    total_attempts=state.total_attempts,
                )
                self._save_cache(cache_path, result)
                return result

            # JavaScript-only shell: render in a headless browser when the optional renderer is available
            if homepage.is_js_shell:
                rendered_html = None
                if self._renderer is not None:
                    rendered_html = await self._renderer.render(successful_root, cfg.read_timeout * 2, user_agent)
                if rendered_html:
                    homepage = self._build_page(successful_root, homepage.status_code, httpx.Headers(), rendered_html)
                    state.notes.append("Homepage rendered with headless browser (JavaScript site)")
                else:
                    state.notes.append("JavaScript-rendered site: static HTML contained minimal text")

            state.homepage = homepage
            state.root_url = successful_root
            state.seen_urls.add(_normalize_page_url(successful_root))
            state.content_hashes.add(_content_hash(homepage.text_content))
            robots = robots_by_host.get(urlparse(successful_root).netloc.lower()) if cfg.respect_robots_txt else None

            if is_social:
                state.notes.append("Social media profile: only publicly visible page metadata evaluated")
                return self._finalize(domain, state, cache_path)

            # Seed pages: supplied landing page(s) and the domain root when crawling started on a subpath
            seed_urls: list[str] = []
            for seed in extra_seed_urls or []:
                if urlparse(seed).netloc.lower().replace("www.", "") == domain:
                    seed_urls.append(seed)
            if urlparse(successful_root).path.strip("/"):
                p = urlparse(successful_root)
                seed_urls.append(f"{p.scheme}://{p.netloc}/")
            if seed_urls:
                await self._fetch_batch(client, seed_urls, gate, state, robots, user_agent)

            # Discover and prioritize subpages from homepage (+ seed) navigation, menus, and footer links
            nav_links: list[tuple[str, str]] | list[str] = list(
                homepage.internal_links_with_anchors or homepage.internal_links
            )
            for sp in state.subpages:
                nav_links.extend(sp.internal_links_with_anchors or sp.internal_links)  # type: ignore[arg-type]
            ranked = self._rank_links(successful_root, nav_links, exclude=state.seen_urls)
            state.discovered_relevant_urls.extend(u for s, u in ranked if s >= 100)
            priority_urls = self._select_priority_subpages(
                successful_root, nav_links, max_count=self.max_subpages, exclude=state.seen_urls
            )
            fetched = await self._fetch_batch(client, priority_urls, gate, state, robots, user_agent)

            # Sitemap discovery of holiday / lighting pages hidden from navigation
            if cfg.use_sitemap:
                sitemap_locs = await self._discover_sitemap_urls(client, successful_root, robots, gate)
                state.sitemap_checked = True
                if sitemap_locs:
                    sm_ranked = self._rank_links(
                        successful_root, [(u, "") for u in sitemap_locs], exclude=state.seen_urls
                    )
                    sm_relevant = [u for s, u in sm_ranked if s >= 100]
                    state.discovered_relevant_urls.extend(u for u in sm_relevant if u not in state.discovered_relevant_urls)
                    sm_pick = sm_relevant[: cfg.max_sitemap_pages]
                    if len(sm_pick) < 2:
                        sm_pick += [u for s, u in sm_ranked if s == 60][: 2 - len(sm_pick)]
                    fetched += await self._fetch_batch(client, sm_pick, gate, state, robots, user_agent)

            # Deep discovery: follow holiday / lighting links found on fetched subpages (two levels)
            frontier = fetched
            for depth in range(2):
                if not frontier:
                    break
                deep_links: list[tuple[str, str]] | list[str] = []
                for sp in frontier:
                    deep_links.extend(sp.internal_links_with_anchors or sp.internal_links)  # type: ignore[arg-type]
                deep_ranked = self._rank_links(successful_root, deep_links, exclude=state.seen_urls)
                min_score = 60 if depth == 0 else 100
                deep_urls = [u for s, u in deep_ranked if s >= min_score][: cfg.max_deep_pages]
                state.discovered_relevant_urls.extend(
                    u for s, u in deep_ranked if s >= 100 and u not in state.discovered_relevant_urls
                )
                frontier = await self._fetch_batch(client, deep_urls, gate, state, robots, user_agent)

            # Proactive probing: if no explicit holiday signal has been found yet, check common holiday service paths
            if not self._has_holiday_signal(state):
                probe_urls = [urljoin(successful_root, p).rstrip("/") for p in HOLIDAY_PROBE_PATHS]
                await self._fetch_batch(
                    client, probe_urls[: max(cfg.max_probe_pages, 1)], gate, state, robots, user_agent
                )
                state.probes_checked = True

        return self._finalize(domain, state, cache_path)

    def _finalize(self, domain: str, state: _CrawlState, cache_path: Path) -> CrawlResult:
        result = self._build_result(domain, state)
        self._save_cache(cache_path, result)
        return result

    def _load_cache(self, path: Path, root_url: str) -> CrawlResult | None:
        """Reuse cached crawl only if it was accessible, complete, and not a bot challenge."""
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.debug(f"Cache read error for {path.name}: {exc}")
            return None

        combined_text = data.get("combined_text", "")
        if (
            not data.get("is_accessible", False)
            or data.get("status_category", "") in ("Bot Challenge", "Timeout", "Connection Error")
            or data.get("incomplete_reason")
            or self._word_count(combined_text) < 40
            or self._is_bot_challenge("", combined_text)
        ):
            return None

        homepage = page_from_dict(data["homepage"]) if data.get("homepage") else None
        return CrawlResult(
            domain=data["domain"],
            base_url=data.get("base_url", root_url),
            is_accessible=True,
            homepage=homepage,
            subpages=[page_from_dict(p) for p in data.get("subpages", [])],
            all_emails=data.get("all_emails", []),
            all_phones=data.get("all_phones", []),
            combined_text=combined_text,
            error_message=data.get("error_message"),
            status_code=data.get("status_code", 200),
            status_category="OK",
            pages_attempted=data.get("pages_attempted", 0),
            pages_fetched=data.get("pages_fetched", 0),
            total_attempts=data.get("total_attempts", 0),
            failed_urls=data.get("failed_urls", []),
            robots_blocked_urls=data.get("robots_blocked_urls", []),
            discovered_relevant_urls=data.get("discovered_relevant_urls", []),
            notes=data.get("notes", []),
            sitemap_checked=data.get("sitemap_checked", False),
            probes_checked=data.get("probes_checked", False),
            from_cache=True,
        )

    def _save_cache(self, path: Path, result: CrawlResult) -> None:
        """Write crawl outcome (including per-page evidence text) to persistent cache."""
        try:
            payload = {
                "domain": result.domain,
                "base_url": result.base_url,
                "is_accessible": result.is_accessible,
                "all_emails": result.all_emails,
                "all_phones": result.all_phones,
                "combined_text": result.combined_text,
                "error_message": result.error_message,
                "status_code": result.status_code,
                "status_category": result.status_category,
                "homepage": page_to_dict(result.homepage) if result.homepage else None,
                "subpages": [page_to_dict(p) for p in result.subpages],
                "pages_attempted": result.pages_attempted,
                "pages_fetched": result.pages_fetched,
                "total_attempts": result.total_attempts,
                "failed_urls": result.failed_urls,
                "robots_blocked_urls": result.robots_blocked_urls,
                "discovered_relevant_urls": result.discovered_relevant_urls,
                "notes": result.notes,
                "incomplete_reason": result.incomplete_reason,
                "sitemap_checked": result.sitemap_checked,
                "probes_checked": result.probes_checked,
                "cached_at": time.time(),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            logger.debug(f"Failed to cache crawl result: {exc}")

    async def _archive_get(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str] | None = None
    ) -> httpx.Response | None:
        """GET from the Internet Archive through the shared polite throttle (None when unavailable)."""
        throttle = self.archive_throttle
        for attempt in range(2):
            if not await throttle.acquire():
                return None
            try:
                resp = await client.get(url, params=params)
            except Exception as exc:
                throttle.record(ok=False)
                logger.debug(f"Wayback request failed for {url}: {exc}")
                await asyncio.sleep(5.0 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                throttle.record(ok=False)
                await asyncio.sleep(10.0 * (attempt + 1))
                continue
            throttle.record(ok=True)
            return resp
        return None

    async def _archive_page(self, client: httpx.AsyncClient, url: str) -> CrawledPage | None:
        resp = await self._archive_get(client, url)
        if resp is None or resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            return None
        return self._build_page(url, 200, resp.headers, resp.text[: self.config.max_page_bytes])

    async def crawl_archived(self, base_url: str, max_pages: int = 2) -> CrawlResult | None:
        """Evaluate the newest usable Wayback Machine snapshot of a site whose live version is unavailable.

        Uses the Internet Archive's public CDX API and raw (`id_`) snapshot URLs through a shared,
        rate-limited throttle; snapshots that captured a bot-challenge or parked page are skipped.
        Returns None when no usable snapshot exists or the archive is unavailable.
        """
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        domain = parsed.netloc.lower().replace("www.", "")
        if not domain or domain in SOCIAL_PROFILE_DOMAINS:
            return None

        headers = dict(DEFAULT_BROWSER_HEADERS)
        if self.custom_user_agent:
            headers["User-Agent"] = self.custom_user_agent

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=40.0, write=10.0, pool=60.0),
            follow_redirects=True,
            max_redirects=8,
            headers=headers,
        ) as client:
            resp = await self._archive_get(
                client,
                "https://web.archive.org/cdx/search/cdx",
                params={
                    "url": domain,
                    "output": "json",
                    "filter": "statuscode:200",
                    "fl": "timestamp,original",
                    "collapse": "timestamp:8",
                    "limit": "-6",
                },
            )
            if resp is None or resp.status_code != 200 or not resp.text.strip():
                return None
            try:
                rows = json.loads(resp.text)[1:]
            except Exception:
                return None

            for ts, original in reversed(rows[-3:]):  # newest first, at most 3 snapshots tried
                snap_url = f"https://web.archive.org/web/{ts}id_/{original}"
                page = await self._archive_page(client, snap_url)
                if page is None or self._word_count(page.text_content) < 40:
                    continue
                head_lower = f"{page.title} {page.text_content[:3000]}".lower()
                if self._is_bot_challenge(page.title, page.text_content) or any(m in head_lower for m in PARKED_DOMAIN_MARKERS):
                    continue

                state = _CrawlState(homepage=page, root_url=snap_url)
                state.content_hashes.add(_content_hash(page.text_content))
                op = urlparse(original)
                original_root = f"{op.scheme or 'https'}://{op.netloc}/"
                ranked = self._rank_links(original_root, page.internal_links_with_anchors or page.internal_links)
                picks = [u for s, u in ranked if s >= 100][:max_pages] or [u for s, u in ranked if s >= 40][:1]
                for link in picks:
                    state.pages_attempted += 1
                    sub = await self._archive_page(client, f"https://web.archive.org/web/{ts}id_/{link}")
                    if sub is not None and self._accept_page(sub, state):
                        state.subpages.append(sub)

                result = self._build_result(domain, state)
                result.archived_snapshot = ts
                result.incomplete_reason = ""  # archive gaps are not evidence of an unevaluable live page
                result.failed_urls = []
                result.notes.append(
                    f"Evaluated Wayback Machine snapshot from {ts[:4]}-{ts[4:6]}-{ts[6:8]} (live site unavailable)"
                )
                return result
        return None

    async def aclose(self) -> None:
        """Release optional headless-browser resources."""
        if self._renderer is not None:
            await self._renderer.close()


class _BrowserRenderer:
    """Optional headless-browser renderer for JavaScript-only sites (requires the `playwright` package)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)
        self._playwright = None
        self._browser = None

    @staticmethod
    def available() -> bool:
        import importlib.util

        try:
            return importlib.util.find_spec("playwright") is not None
        except Exception:
            return False

    async def render(self, url: str, timeout_seconds: float, user_agent: str) -> str | None:
        try:
            from playwright.async_api import async_playwright

            async with self._lock:
                if self._browser is None:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(headless=True)
            async with self._slots:
                context = await self._browser.new_context(user_agent=user_agent)
                try:
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=timeout_seconds * 1000)
                    return await page.content()
                finally:
                    await context.close()
        except Exception as exc:
            logger.debug(f"Headless render failed for {url}: {exc}")
            return None

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._browser = None
        self._playwright = None
