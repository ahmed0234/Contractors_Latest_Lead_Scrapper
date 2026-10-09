"""Targeted, polite website crawler for business verification and contact extraction."""

import asyncio
import hashlib
import html as html_lib
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import get_logger

logger = get_logger("website_crawler")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_REGEX = re.compile(r"(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


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


class WebsiteCrawler:
    """Responsible, polite web crawler for extracting verification signals and contact info.

    Visits homepage and prioritizes /contact, /about, /team, and /locations pages.
    Limits total pages fetched per domain to prevent excessive network use.
    Caches responses to disk deterministically.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout_seconds: float = 8.0,
        max_subpages: int = 5,
    ) -> None:
        self.settings = get_settings()
        self.cache_dir = cache_dir or (self.settings.cache_dir / "crawler")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.max_subpages = max_subpages

    def _get_cache_path(self, domain: str) -> Path:
        """Deterministic cache file for a domain crawl."""
        key = hashlib.sha256(domain.lower().encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _clean_text_from_html(self, html: str) -> tuple[str, str, str, list[str]]:
        """Extract title, meta description, visible body text, footer, and internal links."""
        # Extract title
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else ""
        title = html_lib.unescape(title)

        # Extract meta description
        meta_match = re.search(
            r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
            html,
            re.IGNORECASE,
        )
        meta_desc = meta_match.group(1).strip() if meta_match else ""
        meta_desc = html_lib.unescape(meta_desc)

        # Extract footer specifically so footer contact info is guaranteed in body text
        footer_match = re.search(r"<footer[^>]*>(.*?)</footer>", html, re.IGNORECASE | re.DOTALL)
        footer_text = ""
        if footer_match:
            clean_footer = re.sub(
                r"<(script|style|svg)[^>]*>.*?</\1>", " ", footer_match.group(1), flags=re.I | re.S
            )
            clean_footer = re.sub(r"<[^>]+>", " ", clean_footer)
            footer_text = " " + " ".join(clean_footer.split())

        # Remove script, style, and svg tags
        clean_html = re.sub(
            r"<(script|style|svg|noscript)[^>]*>.*?</\1>",
            " ",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # Strip remaining tags
        text = re.sub(r"<[^>]+>", " ", clean_html)
        text = html_lib.unescape(text)
        # Collapse whitespace
        body_text = " ".join(text.split()) + footer_text

        # Extract href links and dynamically identify contact-related anchors by visible text
        links: list[str] = []
        for m in re.finditer(
            r'<a\s+[^>]*href=["\']([^"\'#\s>]+)["\'][^>]*>(.*?)</a>',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            href = m.group(1).strip()
            anchor_inner = re.sub(r"<[^>]+>", " ", m.group(2)).lower()
            if any(
                term in anchor_inner
                for term in ["contact", "about", "touch", "reach", "location", "email"]
            ):
                links.insert(0, href)
            else:
                links.append(href)

        # Append any remaining hrefs
        all_hrefs = re.findall(r'href=["\']([^"\'#\s>]+)["\']', html, re.IGNORECASE)
        for h in all_hrefs:
            if h not in links:
                links.append(h)

        return title, meta_desc, body_text, links

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

        # 2. mailto: links (including unquoted and URL stripped)
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

        # 4. Direct email regex on body text and unescaped HTML
        raw_candidates.extend(EMAIL_REGEX.findall(text))
        raw_candidates.extend(EMAIL_REGEX.findall(unescaped_html))

        # 5. Text-obfuscated emails (e.g. info [at] domain.com or info (at) domain.com)
        obfuscated = re.findall(
            r"([a-zA-Z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\[@\])\s*([a-zA-Z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\.)\s*([a-zA-Z]{2,})",
            text,
            re.IGNORECASE,
        )
        for user, dom, tld in obfuscated:
            raw_candidates.append(f"{user}@{dom}.{tld}")

        # Filter false positives and dummy emails
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
            # Filter asset extensions
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

        # 6. Extract phones (tel: links + schema + regex)
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

    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> CrawledPage | None:
        """Fetch and parse an individual web page with strict timeout and payload size cap."""
        try:
            # Enforce 5s max per page fetch
            timeout_cfg = httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
            async with client.stream("GET", url, timeout=timeout_cfg) as resp:
                if resp.status_code != 200:
                    return None

                content_type = resp.headers.get("content-type", "").lower()
                if content_type and not any(
                    ct in content_type for ct in ["text", "html", "xml", "json"]
                ):
                    return None

                # Stream up to 1.5 MB max to prevent hanging on huge downloads
                chunks: list[bytes] = []
                total_bytes = 0
                max_bytes = 1572864  # 1.5 MB
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

            title, meta_desc, body_text, links = self._clean_text_from_html(html)
            emails, phones = self._extract_emails_and_phones(html, body_text)

            # Resolve internal links
            base_parsed = urlparse(url)
            internal_links = []
            for link in links:
                full_url = urljoin(url, link)
                target_parsed = urlparse(full_url)
                if target_parsed.netloc.lower() == base_parsed.netloc.lower():
                    internal_links.append(full_url)

            return CrawledPage(
                url=url,
                status_code=resp.status_code,
                title=title,
                meta_description=meta_desc,
                text_content=body_text[:12000],
                emails=emails,
                phones=phones,
                internal_links=list(dict.fromkeys(internal_links)),
            )

        except Exception as exc:
            logger.debug(f"Failed to fetch {url}: {exc}")
            return None

    async def crawl_domain(self, base_url: str) -> CrawlResult:
        """Crawl homepage and priority subpages with a hard 12s overall domain crawl limit."""
        parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
        domain = parsed.netloc.lower().replace("www.", "")
        root_url = f"{parsed.scheme or 'https'}://{parsed.netloc}"

        # 1. Check disk cache
        cache_path = self._get_cache_path(domain)
        if cache_path.exists():
            try:
                with open(cache_path, encoding="utf-8") as f:
                    data = json.load(f)
                    return CrawlResult(
                        domain=data["domain"],
                        base_url=data["base_url"],
                        is_accessible=data["is_accessible"],
                        all_emails=data.get("all_emails", []),
                        all_phones=data.get("all_phones", []),
                        combined_text=data.get("combined_text", ""),
                        error_message=data.get("error_message"),
                    )
            except Exception as exc:
                logger.debug(f"Cache read error for {domain}: {exc}")

        # 2. Perform targeted live crawl within hard overall timeout
        crawl_timeout = min(12.0, max(6.0, self.timeout_seconds * 1.2))
        try:
            return await asyncio.wait_for(
                self._execute_domain_crawl(domain, root_url, cache_path),
                timeout=crawl_timeout,
            )
        except TimeoutError:
            logger.debug(f"Domain crawl timed out for {domain} after {crawl_timeout}s.")
            timeout_result = CrawlResult(
                domain=domain,
                base_url=root_url,
                is_accessible=False,
                error_message="Domain crawl timed out",
            )
            self._save_cache(cache_path, timeout_result)
            return timeout_result
        except Exception as exc:
            logger.debug(f"Domain crawl unexpected error for {domain}: {exc}")
            return CrawlResult(
                domain=domain,
                base_url=root_url,
                is_accessible=False,
                error_message=str(exc),
            )

    async def _execute_domain_crawl(
        self, domain: str, root_url: str, cache_path: Path
    ) -> CrawlResult:
        """Execute the actual network crawl operations across homepage and subpages."""
        combined_text_parts: list[str] = []
        collected_emails: list[str] = []
        collected_phones: list[str] = []
        subpages: list[CrawledPage] = []

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
            follow_redirects=True,
            max_redirects=5,
            verify=False,  # Tolerant to self-signed contractor certificates
            headers={"User-Agent": self.settings.user_agent},
            limits=limits,
        ) as client:
            # Fetch homepage
            homepage = await self._fetch_page(client, root_url)
            if not homepage:
                result = CrawlResult(
                    domain=domain,
                    base_url=root_url,
                    is_accessible=False,
                    error_message="Homepage unreachable or timed out",
                )
                self._save_cache(cache_path, result)
                return result

            combined_text_parts.append(
                f"{homepage.title} {homepage.meta_description} {homepage.text_content}"
            )
            collected_emails.extend(homepage.emails)
            collected_phones.extend(homepage.phones)

            # Select priority subpages
            priority_urls: list[str] = []
            priority_keywords = [
                "/contact",
                "/about",
                "/location",
                "/team",
                "/staff",
                "/reach",
                "/quote",
                "/estimate",
                "/services",
                "/get-in-touch",
                "/find-us",
            ]

            for link in homepage.internal_links:
                link_lower = link.lower()
                if any(k in link_lower for k in priority_keywords):
                    if link not in priority_urls and link != root_url:
                        priority_urls.append(link)
                if len(priority_urls) >= self.max_subpages:
                    break

            # Fallback to standard contact and about paths if still under max_subpages
            for fallback_path in ["/contact", "/contact-us", "/about", "/about-us", "/get-in-touch"]:
                cand_link = urljoin(root_url, fallback_path)
                if cand_link not in priority_urls and cand_link != root_url and len(priority_urls) < self.max_subpages:
                    priority_urls.append(cand_link)

            # Fetch subpages
            for sub_url in priority_urls[: self.max_subpages]:
                sub_page = await self._fetch_page(client, sub_url)
                if sub_page:
                    subpages.append(sub_page)
                    combined_text_parts.append(sub_page.text_content)
                    collected_emails.extend(sub_page.emails)
                    collected_phones.extend(sub_page.phones)

        unique_emails = list(dict.fromkeys(collected_emails))
        unique_phones = list(dict.fromkeys(collected_phones))
        combined_text = " ".join(combined_text_parts)

        result = CrawlResult(
            domain=domain,
            base_url=root_url,
            is_accessible=True,
            homepage=homepage,
            subpages=subpages,
            all_emails=unique_emails,
            all_phones=unique_phones,
            combined_text=combined_text[:35000],
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
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as exc:
            logger.debug(f"Failed to cache crawl result: {exc}")
