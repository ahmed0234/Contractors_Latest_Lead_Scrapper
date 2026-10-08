"""Public and API-driven web search discovery layer for locating contractor websites.

Supports:
1. Commercial Search APIs (Serper.dev, Google Custom Search JSON API, SerpApi) when configured.
2. Resilient Multi-Engine Public Search (DuckDuckGo Lite, DuckDuckGo HTML, Yahoo Search).
3. Directory profile outbound extraction (mining official website links from BBB, Yelp, BuildZoom).
4. Direct synthetic domain DNS probing for exact entity matches.
5. Persistent disk caching of verified candidates (prevents caching empty failures).
"""

import asyncio
import hashlib
import json
import os
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.base import BaseWebsiteFinder, WebsiteCandidate
from contractor_scraper.normalization.cleaners import clean_company_name

logger = get_logger("website_finder")

# Comprehensive blacklist of directories, review platforms, search portals, and aggregators
BLOCKED_DIRECTORY_DOMAINS: set[str] = {
    # Directory & Contractor Portals
    "yelp.com",
    "yellowpages.com",
    "yellowbook.com",
    "bbb.org",
    "angi.com",
    "homeadvisor.com",
    "thumbtack.com",
    "houzz.com",
    "porch.com",
    "buildzoom.com",
    "nextdoor.com",
    "mapquest.com",
    "manta.com",
    "zoominfo.com",
    "dnb.com",
    "chamberofcommerce.com",
    "superpages.com",
    "citysearch.com",
    "dexknows.com",
    "local.com",
    "merchantcircle.com",
    "angieslist.com",
    "bark.com",
    "expertise.com",
    "threebestrated.com",
    "topratedlocal.com",
    "surecritic.com",
    "cylex.us.com",
    "us-info.com",
    "trustpilot.com",
    "contractorlicensecheck.com",
    "cortera.com",
    "opengovus.com",
    "buzzfile.com",
    "datanyze.com",
    "whitepages.com",
    "spokeo.com",
    # Public Registry Scrapers
    "sunbiz.org",
    "floridados.gov",
    "myfloridalicense.com",
    "opencorpdata.com",
    "bizapedia.com",
    "opencorporates.com",
    "floridacompanyregistry.com",
    "flcompanydb.com",
    "corporationwiki.com",
    "floridareg.com",
    # Social & Media
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "pinterest.com",
    "reddit.com",
    "quora.com",
    "wikipedia.org",
    "medium.com",
    "vimeo.com",
    # Search Engines & Portals
    "google.com",
    "duckduckgo.com",
    "bing.com",
    "yahoo.com",
    "msn.com",
    "aol.com",
    # Career & Jobs
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
}

# Directories that frequently list the contractor's official website URL
EXTRACTABLE_DIRECTORIES: tuple[str, ...] = (
    "bbb.org",
    "buildzoom.com",
    "manta.com",
    "chamberofcommerce.com",
    "floridaroof.com",
    "roofingcontractors.org",
    "homeservicelookup.com",
    "findglocal.com",
)

SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_SEARCH_HEADERS = {
    "User-Agent": SEARCH_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}


class HTMLAnchorParser(HTMLParser):
    """Fast standard-library HTML parser to extract all href anchors."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)


def extract_root_domain(url: str) -> str:
    """Extract lowercased root domain (e.g. 'https://www.apexroofing.com/about' -> 'apexroofing.com')."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0].strip()


class WebsiteFinder(BaseWebsiteFinder):
    """Discovers official business website candidates using public search & optional enrichment APIs.

    Features:
    - Commercial Search API integration (Serper, Google Custom Search, SerpApi)
    - Multi-engine public fallback (DuckDuckGo Lite, DDG HTML, Yahoo)
    - Directory outbound link mining
    - High-speed synthetic domain DNS probing
    - Non-empty safe disk caching
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout_seconds: float = 12.0,
        rate_limit_delay: float = 0.3,
    ) -> None:
        self.settings = get_settings()
        self.cache_dir = cache_dir or (self.settings.cache_dir / "discovery")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.rate_limit_delay = rate_limit_delay

    def _get_cache_path(self, company_name: str, city: str, state: str) -> Path:
        """Deterministic cache file path based on company query signature."""
        slug = f"{company_name.lower().strip()}:{city.lower().strip()}:{state.lower().strip()}"
        key = hashlib.sha256(slug.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _is_blocked_domain(self, domain: str) -> bool:
        """Check if domain belongs to a directory, social platform, or aggregator."""
        domain = domain.lower().strip()
        for blocked in BLOCKED_DIRECTORY_DOMAINS:
            if domain == blocked or domain.endswith(f".{blocked}"):
                return True
        return False

    def _calculate_domain_relevance(self, domain: str, company_name: str) -> float:
        """Score how closely a domain name mirrors the company name (0.0 to 1.0)."""
        clean_name = clean_company_name(company_name).lower()
        words = [w for w in re.split(r"[^a-z0-9]", clean_name) if len(w) >= 3]
        if not words:
            return 0.5

        domain_slug = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        matched_words = [w for w in words if w in domain_slug]

        if not matched_words:
            return 0.35

        ratio = len(matched_words) / len(words)
        return min(0.98, 0.5 + (ratio * 0.45))

    async def _search_commercial_api(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Query commercial search APIs if configured (Google CSE, Serper.dev, SerpApi)."""
        results: list[str] = []

        # 1. Serper.dev Google Search API
        serper_key = self.settings.serper_api_key or os.getenv("SERPER_API_KEY")
        if serper_key:
            try:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": serper_key, "Content-Type": "application/json"},
                    json={"q": query, "num": 10},
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    kg = payload.get("knowledgeGraph", {})
                    if kg.get("website"):
                        results.append(kg["website"])
                    for org in payload.get("organic", []):
                        if org.get("link"):
                            results.append(org["link"])
                    if results:
                        return results
            except Exception as exc:
                logger.debug(f"Serper API query failed: {exc}")

        # 2. Google Custom Search JSON API
        google_key = (
            self.settings.google_search_api_key
            or os.getenv("GOOGLE_SEARCH_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
        )
        google_cx = (
            self.settings.google_search_cx
            or os.getenv("GOOGLE_SEARCH_CX")
            or os.getenv("GOOGLE_CSE_ID")
        )
        if google_key and google_cx:
            try:
                resp = await client.get(
                    "https://customsearch.googleapis.com/customsearch/v1",
                    params={"key": google_key, "cx": google_cx, "q": query, "num": 10},
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("items", []):
                        if item.get("link"):
                            results.append(item["link"])
                    if results:
                        return results
            except Exception as exc:
                logger.debug(f"Google CSE query failed: {exc}")

        # 3. SerpApi
        serpapi_key = self.settings.serpapi_api_key or os.getenv("SERPAPI_API_KEY")
        if serpapi_key:
            try:
                resp = await client.get(
                    "https://serpapi.com/search.json",
                    params={"engine": "google", "api_key": serpapi_key, "q": query, "num": 10},
                    timeout=8.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("organic_results", []):
                        if item.get("link"):
                            results.append(item["link"])
                    if results:
                        return results
            except Exception as exc:
                logger.debug(f"SerpApi query failed: {exc}")

        return results

    async def _search_ddg_lite(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search DuckDuckGo Lite endpoint (clean HTML, highly resilient to JS challenges)."""
        try:
            resp = await client.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query},
                headers=DEFAULT_SEARCH_HEADERS,
            )
            if resp.status_code != 200:
                return []

            parser = HTMLAnchorParser()
            parser.feed(resp.text)
            links: list[str] = []
            for href in parser.hrefs:
                actual = href
                if "uddg=" in href:
                    target = parse_qs(urlparse(href).query).get("uddg")
                    if target:
                        actual = unquote(target[0])
                if actual.startswith("http") and "duckduckgo.com" not in actual:
                    links.append(actual)
            return list(dict.fromkeys(links))
        except Exception as exc:
            logger.debug(f"DDG Lite search error: {exc}")
            return []

    async def _search_ddg_html(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search DuckDuckGo HTML endpoint."""
        try:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers=DEFAULT_SEARCH_HEADERS,
            )
            if resp.status_code != 200:
                return []

            raw_urls = re.findall(
                r'href=["\'](?:/l/\?uddg=)?(https?://[^"\']+)["\']',
                resp.text,
            )
            links: list[str] = []
            for raw_url in raw_urls:
                actual_url = raw_url
                if "uddg=" in raw_url:
                    parsed_target = parse_qs(urlparse(raw_url).query).get("uddg")
                    if parsed_target:
                        actual_url = unquote(parsed_target[0])
                if actual_url.startswith("http") and "duckduckgo.com" not in actual_url:
                    links.append(actual_url)
            return list(dict.fromkeys(links))
        except Exception as exc:
            logger.debug(f"DDG HTML search error: {exc}")
            return []

    async def _search_yahoo(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search Yahoo search as a reliable public secondary fallback."""
        try:
            resp = await client.get(
                "https://search.yahoo.com/search",
                params={"p": query},
                headers=DEFAULT_SEARCH_HEADERS,
            )
            if resp.status_code != 200:
                return []

            parser = HTMLAnchorParser()
            parser.feed(resp.text)
            links: list[str] = []
            for href in parser.hrefs:
                if "r.search.yahoo.com" in href and "/RU=" in href:
                    m = re.search(r"/RU=([^/]+)/RK=", href)
                    if m:
                        target = unquote(m.group(1))
                        if target.startswith("http") and "yahoo.com" not in target:
                            links.append(target)
                elif href.startswith("http") and not any(
                    y in href for y in ["yahoo.com", "yimg.com", "advertising.com"]
                ):
                    links.append(href)
            return list(dict.fromkeys(links))
        except Exception as exc:
            logger.debug(f"Yahoo search error: {exc}")
            return []

    async def _extract_directory_website(
        self, client: httpx.AsyncClient, directory_url: str
    ) -> str | None:
        """Inspect a business directory listing to find the company's official outbound website."""
        try:
            resp = await client.get(
                directory_url,
                headers=DEFAULT_SEARCH_HEADERS,
                timeout=6.0,
            )
            if resp.status_code != 200:
                return None

            parser = HTMLAnchorParser()
            parser.feed(resp.text)
            for link in parser.hrefs:
                if not link.startswith("http"):
                    continue
                dom = extract_root_domain(link)
                if not dom or self._is_blocked_domain(dom):
                    continue
                # Found candidate official domain from directory listing
                return f"https://{dom}"
        except Exception:
            pass
        return None

    def _generate_synthetic_domains(
        self, company_name: str, city: str, state: str = "FL"
    ) -> list[str]:
        """Synthesize plausible official domain variations based on legal business name and location."""
        clean = clean_company_name(company_name).lower()
        words = [w for w in re.split(r"[^a-z0-9]", clean) if w and len(w) >= 2]
        if not words:
            return []

        joined = "".join(words)
        dashed = "-".join(words)
        c_clean = re.sub(r"[^a-z0-9]", "", city.lower())
        st_clean = state.lower().strip()

        candidates = [
            f"{joined}.com",
            f"{dashed}.com",
            f"{joined}{st_clean}.com",
            f"{joined}{c_clean}.com",
            f"{c_clean}{joined}.com",
            f"{joined}.net",
        ]
        return list(dict.fromkeys(candidates))

    async def _probe_dns(self, domain: str) -> bool:
        """Perform high-speed asynchronous DNS resolution check for domain existence."""
        try:
            loop = asyncio.get_running_loop()
            await loop.getaddrinfo(domain, 80, family=socket.AF_INET)
            return True
        except Exception:
            return False

    async def find_websites(
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        category: str = "roofing",
    ) -> list[WebsiteCandidate]:
        """Search web indices and synthetic probes for official candidate websites."""
        # 1. Check persistent cache (ONLY accept non-empty results!)
        cache_file = self._get_cache_path(company_name, city, state)
        if cache_file.exists():
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                    if data and len(data) > 0:
                        return [WebsiteCandidate(**item) for item in data]
            except Exception as exc:
                logger.debug(f"Failed to load discovery cache: {exc}")

        clean_name = clean_company_name(company_name)
        candidates: list[WebsiteCandidate] = []
        seen_domains: set[str] = set()
        directory_urls: list[str] = []

        queries = [
            f'"{clean_name}" {city} {state}',
            f"{clean_name} {category} {city} {state}",
        ]

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            verify=False,
            headers=DEFAULT_SEARCH_HEADERS,
        ) as client:
            # 2. Try Commercial Search APIs first (if keys provided)
            for query in queries:
                api_links = await self._search_commercial_api(client, query)
                for link in api_links:
                    dom = extract_root_domain(link)
                    if not dom or dom in seen_domains:
                        continue
                    if self._is_blocked_domain(dom):
                        if any(ext in dom for ext in EXTRACTABLE_DIRECTORIES):
                            directory_urls.append(link)
                        continue
                    seen_domains.add(dom)
                    relevance = self._calculate_domain_relevance(dom, clean_name)
                    candidates.append(
                        WebsiteCandidate(
                            url=f"https://{dom}",
                            domain=dom,
                            discovery_source="commercial_api",
                            initial_confidence=relevance,
                        )
                    )
                if len(candidates) >= 3:
                    break

            # 3. Public Multi-Engine Web Search (DuckDuckGo Lite, DDG HTML, Yahoo)
            if not candidates:
                for query in queries:
                    # 3a. DuckDuckGo Lite
                    search_links = await self._search_ddg_lite(client, query)
                    # 3b. DuckDuckGo HTML fallback
                    if not search_links:
                        search_links = await self._search_ddg_html(client, query)
                    # 3c. Yahoo Search fallback
                    if not search_links:
                        search_links = await self._search_yahoo(client, query)

                    for link in search_links:
                        dom = extract_root_domain(link)
                        if not dom or dom in seen_domains:
                            continue
                        if self._is_blocked_domain(dom):
                            if any(ext in dom for ext in EXTRACTABLE_DIRECTORIES):
                                directory_urls.append(link)
                            continue

                        seen_domains.add(dom)
                        relevance = self._calculate_domain_relevance(dom, clean_name)
                        candidates.append(
                            WebsiteCandidate(
                                url=f"https://{dom}",
                                domain=dom,
                                discovery_source="public_search",
                                initial_confidence=relevance,
                            )
                        )

                    if len(candidates) >= 3:
                        break

            # 4. Directory Outbound Link Extraction (if only directory pages were returned)
            if not candidates and directory_urls:
                for dir_url in directory_urls[:3]:
                    extracted_url = await self._extract_directory_website(client, dir_url)
                    if extracted_url:
                        dom = extract_root_domain(extracted_url)
                        if dom and dom not in seen_domains and not self._is_blocked_domain(dom):
                            seen_domains.add(dom)
                            relevance = self._calculate_domain_relevance(dom, clean_name)
                            candidates.append(
                                WebsiteCandidate(
                                    url=f"https://{dom}",
                                    domain=dom,
                                    discovery_source="directory_outbound",
                                    initial_confidence=max(0.70, relevance),
                                )
                            )
                            if len(candidates) >= 2:
                                break

            # 5. Synthetic Domain Probing Fallback
            if not candidates:
                synthetic_domains = self._generate_synthetic_domains(clean_name, city, state)
                for dom in synthetic_domains:
                    if dom in seen_domains:
                        continue
                    if await self._probe_dns(dom):
                        seen_domains.add(dom)
                        relevance = self._calculate_domain_relevance(dom, clean_name)
                        candidates.append(
                            WebsiteCandidate(
                                url=f"https://{dom}",
                                domain=dom,
                                discovery_source="synthetic_probe",
                                initial_confidence=max(0.75, relevance),
                            )
                        )
                        if len(candidates) >= 2:
                            break

        # Sort candidates descending by confidence score
        candidates.sort(key=lambda c: c.initial_confidence, reverse=True)

        # 6. Cache results to disk ONLY when valid candidates are discovered
        if candidates:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump([c.__dict__ for c in candidates], f, indent=2)
            except Exception as exc:
                logger.debug(f"Failed to write discovery cache: {exc}")

        return candidates
