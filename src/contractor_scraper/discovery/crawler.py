"""Targeted, polite, and resilient website crawler for business verification and contact extraction."""

import asyncio
import hashlib
import html as html_lib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
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
    ) -> None:
        self.settings = get_settings()
        self.cache_dir = cache_dir or (self.settings.cache_dir / "crawler")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.max_subpages = max_subpages
        self.priority_keywords = priority_keywords
        self.custom_user_agent = custom_user_agent

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

    def _is_bot_challenge(self, title: str, text: str) -> bool:
        """Detect whether page content is a bot challenge or JavaScript interstitial."""
        combined = f"{title} {text[:2000]}".lower()
        patterns = (
            "one moment, please",
            "please wait while your request is being verified",
            "just a moment...",
            "attention required! | cloudflare",
            "access denied",
            "sgcaptcha",
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
        text = re.sub(r"<[^>]+>", " ", clean_html)
        text = html_lib.unescape(text)
        body_text = " ".join(text.split()) + footer_text + schema_text

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

    def _select_priority_subpages(
        self,
        root_url: str,
        links: list[tuple[str, str]] | list[str],
        max_count: int,
    ) -> list[str]:
        """Categorize internal links into priority tiers and select the most relevant pages."""
        base_parsed = urlparse(root_url)
        base_netloc = base_parsed.netloc.lower().replace("www.", "")

        tier1_holiday: list[str] = []
        tier2_exterior: list[str] = []
        tier3_services: list[str] = []
        tier4_about: list[str] = []

        seen_urls: set[str] = {root_url.rstrip("/")}

        for item in links:
            if isinstance(item, tuple):
                href, anchor = item
            else:
                href, anchor = str(item), ""

            if not href or href.startswith("mailto:") or href.startswith("tel:") or href.startswith("javascript:"):
                continue
            full_url = urljoin(root_url, href)
            target_parsed = urlparse(full_url)
            target_netloc = target_parsed.netloc.lower().replace("www.", "")

            # Verify same domain
            if target_netloc != base_netloc:
                continue

            # Strip query/fragment
            clean_url = f"{target_parsed.scheme}://{target_parsed.netloc}{target_parsed.path}".rstrip("/")
            if not clean_url or clean_url in seen_urls:
                continue

            url_path_lower = target_parsed.path.lower()
            combined_match_str = f"{url_path_lower} {anchor}"

            # Tier 1: Explicit Christmas / Holiday / Seasonal terms
            if any(
                k in combined_match_str
                for k in [
                    "christmas",
                    "holiday",
                    "xmas",
                    "seasonal",
                    "hang",
                    "decor",
                    "takedown",
                    "c9",
                    "roofline",
                    "trimlight",
                    "gemstone",
                    "jellyfish",
                    "everlights",
                    "invisa-lite",
                    "celebright",
                    "oelo",
                    "permanent-holiday",
                    "permanent-lighting",
                    "winter-services",
                ]
            ):
                tier1_holiday.append(clean_url)
                seen_urls.add(clean_url)

            # Tier 2: Exterior, Decorative, or Permanent Lighting terms
            elif any(
                k in combined_match_str
                for k in [
                    "outdoor-lighting",
                    "exterior-lighting",
                    "decorative-lighting",
                    "landscape-lighting",
                    "trimlight",
                    "gemstone",
                    "jellyfish",
                    "everlights",
                    "lighting",
                    "light",
                ]
            ):
                tier2_exterior.append(clean_url)
                seen_urls.add(clean_url)

            # Tier 3: Core Service Pages
            elif any(
                k in combined_match_str
                for k in [
                    "services",
                    "service",
                    "what-we-do",
                    "our-services",
                    "residential",
                    "commercial",
                    "winter",
                    "seasonal",
                ]
            ):
                tier3_services.append(clean_url)
                seen_urls.add(clean_url)

            # Tier 4: About, Contact, Quote
            elif any(
                k in combined_match_str
                for k in [
                    "about",
                    "contact",
                    "quote",
                    "estimate",
                    "location",
                ]
            ):
                tier4_about.append(clean_url)
                seen_urls.add(clean_url)

        # Assemble prioritized selection
        selected: list[str] = []
        for tier in [tier1_holiday, tier2_exterior, tier3_services, tier4_about]:
            for u in tier:
                if len(selected) < max_count:
                    selected.append(u)
                else:
                    break
            if len(selected) >= max_count:
                break

        # Fallback well-known paths if fewer found
        if len(selected) < max_count:
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
                if fb_url not in seen_urls and len(selected) < max_count:
                    selected.append(fb_url)
                    seen_urls.add(fb_url)

        return selected

    async def _fetch_page(
        self, client: httpx.AsyncClient, url: str
    ) -> CrawledPage | None:
        """Fetch and parse an individual web page with resilient timeouts and payload caps."""
        try:
            # 6s connect, 10s read
            timeout_cfg = httpx.Timeout(connect=6.0, read=10.0, write=5.0, pool=5.0)
            async with client.stream("GET", url, timeout=timeout_cfg) as resp:
                # Handle rate limiting retry
                if resp.status_code in (429, 503):
                    await asyncio.sleep(1.5)
                    async with client.stream("GET", url, timeout=timeout_cfg) as retry_resp:
                        resp = retry_resp

                if resp.status_code not in (200, 201, 202, 203, 301, 302, 403):
                    return None

                content_type = resp.headers.get("content-type", "").lower()
                if content_type and not any(
                    ct in content_type for ct in ["text", "html", "xml", "json"]
                ):
                    return None

                # Stream up to 2 MB max
                chunks: list[bytes] = []
                total_bytes = 0
                max_bytes = 2097152  # 2.0 MB
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    total_bytes += len(chunk)
                    if total_bytes >= max_bytes:
                        break

                raw_bytes = b"".join(chunks)
                encoding = resp.encoding or "utf-8"
                try:
                    html = raw_bytes.decode(encoding, errors="replace")
                except Exception:
                    html = raw_bytes.decode("utf-8", errors="replace")

            title, meta_desc, body_text, clean_links = self._clean_text_from_html(html)
            links_with_anchors = self._extract_links_with_anchors(html)
            emails, phones = self._extract_emails_and_phones(html, body_text)

            return CrawledPage(
                url=url,
                status_code=resp.status_code,
                title=title,
                meta_description=meta_desc,
                text_content=body_text[:35000],
                emails=emails,
                phones=phones,
                internal_links=clean_links,
                internal_links_with_anchors=links_with_anchors,
            )

        except Exception as exc:
            logger.debug(f"Failed to fetch {url}: {exc}")
            return None

    async def crawl_domain(self, base_url: str) -> CrawlResult:
        """Crawl homepage and priority subpages with recovery fallbacks and overall timeout."""
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

        # 1. Check disk cache
        cache_path = self._get_cache_path(domain)
        if cache_path.exists():
            try:
                with open(cache_path, encoding="utf-8") as f:
                    data = json.load(f)
                    # Only reuse cache if it was accessible and not a bot challenge / empty failure
                    is_acc = data.get("is_accessible", False)
                    status_cat = data.get("status_category", "")
                    combined_text = data.get("combined_text", "")
                    if (
                        is_acc
                        and status_cat not in ("Bot Challenge", "Timeout", "Connection Error")
                        and len(combined_text.split()) >= 40
                        and not self._is_bot_challenge("", combined_text)
                    ):
                        return CrawlResult(
                            domain=data["domain"],
                            base_url=data.get("base_url", root_url),
                            is_accessible=True,
                            all_emails=data.get("all_emails", []),
                            all_phones=data.get("all_phones", []),
                            combined_text=combined_text,
                            error_message=data.get("error_message"),
                            status_code=data.get("status_code", 200),
                            status_category="OK",
                        )
            except Exception as exc:
                logger.debug(f"Cache read error for {domain}: {exc}")

        # 2. Perform live resilient crawl
        crawl_timeout = min(30.0, max(15.0, self.timeout_seconds * 2.0))
        try:
            return await asyncio.wait_for(
                self._execute_domain_crawl(domain, base_url, cache_path),
                timeout=crawl_timeout,
            )
        except TimeoutError:
            logger.debug(f"Domain crawl timed out for {domain} after {crawl_timeout}s.")
            timeout_result = CrawlResult(
                domain=domain,
                base_url=root_url,
                is_accessible=False,
                error_message="Domain crawl timed out across all connection attempts",
                status_category="Timeout",
            )
            return timeout_result
        except Exception as exc:
            logger.debug(f"Domain crawl unexpected error for {domain}: {exc}")
            err_cat = "Timeout" if "timeout" in str(exc).lower() else "Connection Error"
            error_result = CrawlResult(
                domain=domain,
                base_url=root_url,
                is_accessible=False,
                error_message=str(exc),
                status_category=err_cat,
            )
            return error_result

    async def _execute_domain_crawl(
        self, domain: str, base_url: str, cache_path: Path
    ) -> CrawlResult:
        """Execute network crawl operations with multi-candidate fallbacks and parallel subpages."""
        combined_text_parts: list[str] = []
        collected_emails: list[str] = []
        collected_phones: list[str] = []
        subpages: list[CrawledPage] = []

        headers = dict(DEFAULT_BROWSER_HEADERS)
        if self.custom_user_agent:
            headers["User-Agent"] = self.custom_user_agent

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

        # Generate recovery candidates (https/http, www/non-www)
        candidates = self._generate_candidate_urls(base_url)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=6.0, read=10.0, write=5.0, pool=5.0),
            follow_redirects=True,
            max_redirects=5,
            verify=False,  # Tolerant to self-signed or expired contractor certificates
            headers=headers,
            limits=limits,
        ) as client:
            homepage: CrawledPage | None = None
            successful_root: str = base_url
            last_error: str = "Connection failed"
            last_status_cat: str = "Connection Error"
            last_status_code: int | None = None

            # Attempt each candidate URL
            for cand_url in candidates:
                try:
                    page = await self._fetch_page(client, cand_url)
                    if page:
                        homepage = page
                        successful_root = cand_url
                        last_status_code = page.status_code
                        break
                except httpx.TimeoutException as exc:
                    last_status_cat = "Timeout"
                    last_error = f"Timeout connecting to {cand_url}: {exc}"
                except httpx.HTTPError as exc:
                    last_status_cat = "Connection Error"
                    last_error = f"HTTP error on {cand_url}: {exc}"
                except Exception as exc:
                    last_status_cat = "Connection Error"
                    last_error = f"Connection failed on {cand_url}: {exc}"

            if not homepage:
                result = CrawlResult(
                    domain=domain,
                    base_url=base_url,
                    is_accessible=False,
                    error_message=last_error,
                    status_code=last_status_code,
                    status_category=last_status_cat,
                )
                self._save_cache(cache_path, result)
                return result

            # Check for Bot Challenge / JavaScript Interstitial
            if self._is_bot_challenge(homepage.title, homepage.text_content) or homepage.status_code == 403:
                # Attempt fallback with Bot User-Agent (often permitted by firewalls for indexing)
                try:
                    bot_page = await self._fetch_page(
                        client,
                        successful_root,
                    )
                    # Try with alternate Bot headers
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
                        error_message="Website protected by web firewall / Cloudflare bot challenge",
                        status_code=homepage.status_code,
                        status_category="Bot Challenge",
                    )
                    self._save_cache(cache_path, result)
                    return result

            combined_text_parts.append(
                f"{homepage.title} {homepage.meta_description} {homepage.text_content}"
            )
            collected_emails.extend(homepage.emails)
            collected_phones.extend(homepage.phones)

            # Discover and prioritize subpages
            priority_urls = self._select_priority_subpages(
                successful_root,
                homepage.internal_links_with_anchors or homepage.internal_links,
                max_count=self.max_subpages,
            )

            # Fetch priority subpages concurrently for high performance
            seen_crawled_urls = set(priority_urls) | {successful_root.rstrip("/")}
            if priority_urls:
                sub_tasks = [self._fetch_page(client, s_url) for s_url in priority_urls]
                fetched_subpages = await asyncio.gather(*sub_tasks, return_exceptions=True)
                for sp in fetched_subpages:
                    if isinstance(sp, CrawledPage) and sp:
                        subpages.append(sp)
                        combined_text_parts.append(f"{sp.title} {sp.text_content}")
                        collected_emails.extend(sp.emails)
                        collected_phones.extend(sp.phones)

                # Tier-2 Deep Discovery: Inspect internal links from fetched subpages
                # (e.g. /services page linking to /services/holiday-lighting)
                deep_holiday_urls: list[str] = []
                for sp in fetched_subpages:
                    if isinstance(sp, CrawledPage) and sp:
                        for deep_link in (sp.internal_links_with_anchors or sp.internal_links):
                            href = deep_link[0] if isinstance(deep_link, tuple) else str(deep_link)
                            anchor = deep_link[1] if isinstance(deep_link, tuple) else ""
                            if not href or href.startswith("mailto:") or href.startswith("tel:"):
                                continue
                            full_deep_url = urljoin(successful_root, href).rstrip("/")
                            if urlparse(full_deep_url).netloc.lower().replace("www.", "") != domain:
                                continue
                            if full_deep_url in seen_crawled_urls:
                                continue
                            comb = f"{full_deep_url.lower()} {anchor.lower()}"
                            if any(k in comb for k in ["christmas", "holiday", "xmas", "seasonal", "takedown", "trimlight", "gemstone", "jellyfish", "roofline"]):
                                deep_holiday_urls.append(full_deep_url)
                                seen_crawled_urls.add(full_deep_url)

                if deep_holiday_urls:
                    deep_tasks = [self._fetch_page(client, du) for du in deep_holiday_urls[:4]]
                    deep_fetched = await asyncio.gather(*deep_tasks, return_exceptions=True)
                    for dsp in deep_fetched:
                        if isinstance(dsp, CrawledPage) and dsp:
                            subpages.append(dsp)
                            combined_text_parts.append(f"{dsp.title} {dsp.text_content}")
                            collected_emails.extend(dsp.emails)
                            collected_phones.extend(dsp.phones)

            # Proactive probing: if no explicit holiday signal has been found yet, check common holiday service paths
            current_combined_lower = " ".join(combined_text_parts).lower()
            has_holiday_signal = any(
                k in current_combined_lower
                for k in ["christmas", "holiday", "xmas", "trimlight", "gemstone", "jellyfish"]
            )
            if not has_holiday_signal:
                common_probes = [
                    "/holiday-lighting",
                    "/christmas-light-installation",
                    "/christmas-lights",
                    "/services/holiday-lighting",
                    "/services/christmas-lights",
                    "/holiday-lights",
                    "/permanent-holiday-lighting",
                    "/trimlight",
                ]
                probe_urls = [
                    urljoin(successful_root, cp).rstrip("/")
                    for cp in common_probes
                    if urljoin(successful_root, cp).rstrip("/") not in seen_crawled_urls
                ]
                if probe_urls:
                    probe_tasks = [self._fetch_page(client, pu) for pu in probe_urls[:4]]
                    probe_fetched = await asyncio.gather(*probe_tasks, return_exceptions=True)
                    for psp in probe_fetched:
                        if isinstance(psp, CrawledPage) and psp and len(psp.text_content.split()) >= 30:
                            if not any(e in (psp.title + psp.text_content[:200]).lower() for e in ["page not found", "404 not found", "error 404"]):
                                subpages.append(psp)
                                combined_text_parts.append(f"{psp.title} {psp.text_content}")
                                collected_emails.extend(psp.emails)
                                collected_phones.extend(psp.phones)

        unique_emails = list(dict.fromkeys(collected_emails))
        unique_phones = list(dict.fromkeys(collected_phones))
        combined_text = " ".join(combined_text_parts)

        result = CrawlResult(
            domain=domain,
            base_url=successful_root,
            is_accessible=True,
            homepage=homepage,
            subpages=subpages,
            all_emails=unique_emails,
            all_phones=unique_phones,
            combined_text=combined_text[:90000],
            status_code=homepage.status_code if homepage else 200,
            status_category="OK",
        )
        self._save_cache(cache_path, result)
        return result

    def _save_cache(self, path: Path, result: CrawlResult) -> None:
        """Write crawl outcome to persistent cache."""
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
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            logger.debug(f"Failed to cache crawl result: {exc}")
