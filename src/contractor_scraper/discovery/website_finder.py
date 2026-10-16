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
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core import progress as prg
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
    # Search Engines & Tech Portals
    "google.com",
    "duckduckgo.com",
    "bing.com",
    "yahoo.com",
    "msn.com",
    "aol.com",
    "microsoft.com",
    "live.com",
    "office.com",
    "apple.com",
    # Encyclopedias & Travel Aggregators
    "worldatlas.com",
    "traveltourister.com",
    "britannica.com",
    "tripadvisor.com",
    # Associations & Trade Portals (Extractable outbound or blocked)
    "floridaroof.com",
    "floridacontractorcheck.com",
    "floridaconstructionguide.com",
    # Pest Control Portals & Associations
    "pestworld.org",
    "npmapestworld.org",
    "pctonline.com",
    "mypmp.net",
    "pestcontroltechnology.com",
    "peststrategies.com",
    "qualitypro.org",
    "certifiedpestcontrol.com",
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

PARKED_DOMAIN_SIGNATURES: tuple[str, ...] = (
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


CAPTCHA_SIGNATURES: tuple[str, ...] = (
    "cf-challenge",
    "challenges.cloudflare.com",
    "recaptcha",
    "g-recaptcha",
    "hcaptcha",
    "captcha",
    "unusual traffic",
    "robot",
    "bot detection",
    "please verify you are a human",
    "access denied",
    "blocked",
    "distil_r_captcha",
    "perimeterx",
)


class SearchProviderTracker:
    """Tracks rate limiting, CAPTCHA blocks, timeouts, and cooldown backoffs per search provider."""

    def __init__(self, default_cooldown_seconds: float = 60.0) -> None:
        self.default_cooldown = default_cooldown_seconds
        self.cooldown_until: dict[str, float] = {}
        self.unconfigured_reported: set[str] = set()

    def is_available(self, provider: str) -> bool:
        """Returns True if the provider is not currently in a backoff cooldown window."""
        until = self.cooldown_until.get(provider, 0.0)
        return time.time() >= until

    def get_remaining_cooldown(self, provider: str) -> float:
        until = self.cooldown_until.get(provider, 0.0)
        return max(0.0, until - time.time())

    def mark_blocked(
        self,
        provider: str,
        reason: str = "rate_limit",
        duration_seconds: float | None = None,
    ) -> None:
        """Apply backoff cooldown window so scraper stops repeatedly hammering this provider."""
        cooldown = duration_seconds or self.default_cooldown
        self.cooldown_until[provider] = time.time() + cooldown
        logger.warning(
            f"Provider '{provider}' marked in backoff for {cooldown:.0f}s due to: {reason}. Switching to alternative provider."
        )

    def mark_success(self, provider: str) -> None:
        """Provider responded successfully."""
        self.cooldown_until.pop(provider, None)


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
        tracker: SearchProviderTracker | None = None,
    ) -> None:
        self.settings = get_settings()
        self.cache_dir = cache_dir or (self.settings.cache_dir / "discovery")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.rate_limit_delay = rate_limit_delay
        self.tracker = tracker or SearchProviderTracker()

    def _get_cache_path(self, company_name: str, city: str, state: str) -> Path:
        """Deterministic cache file path based on company query signature."""
        slug = f"{company_name.lower().strip()}:{city.lower().strip()}:{state.lower().strip()}"
        key = hashlib.sha256(slug.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    DISALLOWED_TLD_SUFFIXES: set[str] = {
        ".pk", ".in", ".cn", ".ru", ".de", ".fr", ".br", ".au", ".jp", ".kr", ".it",
        ".nl", ".es", ".gov", ".mil", ".edu", ".gov.pk", ".gov.in", ".gov.uk"
    }

    def _is_blocked_domain(self, domain: str) -> bool:
        """Check if domain belongs to a directory, social platform, aggregator, or non-US government/foreign TLD."""
        domain = domain.lower().strip()
        for blocked in BLOCKED_DIRECTORY_DOMAINS:
            if domain == blocked or domain.endswith(f".{blocked}"):
                return True
        for tld in self.DISALLOWED_TLD_SUFFIXES:
            if domain.endswith(tld):
                return True
        return False

    def _calculate_domain_relevance(self, domain: str, company_name: str) -> float:
        """Score how closely a domain name mirrors the company name (0.0 to 1.0)."""
        clean_name = clean_company_name(company_name).lower()
        words = [w for w in re.split(r"[^a-z0-9]", clean_name) if len(w) >= 3]
        if not words:
            # Fallback to shorter words if all are short (e.g. 2-letter tokens)
            words = [w for w in re.split(r"[^a-z0-9]", clean_name) if len(w) >= 2]
        if not words:
            return 0.0

        domain_slug = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
        matched_words = [w for w in words if w in domain_slug]

        if not matched_words:
            return 0.0

        ratio = len(matched_words) / len(words)
        return min(0.98, 0.45 + (ratio * 0.50))

    async def _search_commercial_api(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Query commercial search APIs if configured (Google CSE, Serper.dev, SerpApi)."""
        results: list[str] = []

        # 1. Serper.dev Google Search API
        serper_key = self.settings.serper_api_key or os.getenv("SERPER_API_KEY")
        if serper_key:
            if self.tracker.is_available("Serper.dev"):
                prg.print_search_start("Serper.dev", query)
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
                            self.tracker.mark_success("Serper.dev")
                            prg.print_search_success("Serper.dev", len(results))
                            return results
                        else:
                            prg.print_search_empty("Serper.dev")
                    elif resp.status_code == 429:
                        prg.print_search_rate_limited("Serper.dev", resp.status_code)
                        self.tracker.mark_blocked("Serper.dev", "HTTP 429", duration_seconds=60.0)
                    else:
                        prg.print_search_error("Serper.dev", f"HTTP {resp.status_code}")
                except (httpx.TimeoutException, TimeoutError):
                    prg.print_search_timeout("Serper.dev")
                except Exception as exc:
                    prg.print_search_error("Serper.dev", str(exc))
        else:
            if "Serper.dev" not in self.tracker.unconfigured_reported:
                prg.print_api_not_configured("Serper.dev")
                self.tracker.unconfigured_reported.add("Serper.dev")

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
            if self.tracker.is_available("Google CSE"):
                prg.print_search_start("Google CSE", query)
                try:
                    resp = await client.get(
                        "https://customsearch.googleapis.com/customsearch/v1",
                        params={"key": google_key, "cx": google_cx, "q": query, "num": 10},
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("items", []):
                            if item.get("link"):
                                results.append(item["link"])
                        if results:
                            self.tracker.mark_success("Google CSE")
                            prg.print_search_success("Google CSE", len(results))
                            return results
                        else:
                            prg.print_search_empty("Google CSE")
                    elif resp.status_code == 429:
                        prg.print_search_rate_limited("Google CSE", resp.status_code)
                        self.tracker.mark_blocked("Google CSE", "HTTP 429", duration_seconds=60.0)
                    else:
                        prg.print_search_error("Google CSE", f"HTTP {resp.status_code}")
                except (httpx.TimeoutException, TimeoutError):
                    prg.print_search_timeout("Google CSE")
                except Exception as exc:
                    prg.print_search_error("Google CSE", str(exc))
        else:
            if "Google CSE" not in self.tracker.unconfigured_reported:
                prg.print_api_not_configured("Google CSE")
                self.tracker.unconfigured_reported.add("Google CSE")

        # 3. SerpApi
        serpapi_key = self.settings.serpapi_api_key or os.getenv("SERPAPI_API_KEY")
        if serpapi_key:
            if self.tracker.is_available("SerpApi"):
                prg.print_search_start("SerpApi", query)
                try:
                    resp = await client.get(
                        "https://serpapi.com/search.json",
                        params={"engine": "google", "api_key": serpapi_key, "q": query, "num": 10},
                        timeout=5.0,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        for item in data.get("organic_results", []):
                            if item.get("link"):
                                results.append(item["link"])
                        if results:
                            self.tracker.mark_success("SerpApi")
                            prg.print_search_success("SerpApi", len(results))
                            return results
                        else:
                            prg.print_search_empty("SerpApi")
                    elif resp.status_code == 429:
                        prg.print_search_rate_limited("SerpApi", resp.status_code)
                        self.tracker.mark_blocked("SerpApi", "HTTP 429", duration_seconds=60.0)
                    else:
                        prg.print_search_error("SerpApi", f"HTTP {resp.status_code}")
                except (httpx.TimeoutException, TimeoutError):
                    prg.print_search_timeout("SerpApi")
                except Exception as exc:
                    prg.print_search_error("SerpApi", str(exc))
        else:
            if "SerpApi" not in self.tracker.unconfigured_reported:
                prg.print_api_not_configured("SerpApi")
                self.tracker.unconfigured_reported.add("SerpApi")

        return results

    async def _search_ddg_lite(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search DuckDuckGo Lite endpoint with fast 2.5s timeout."""
        provider = "DuckDuckGo Lite"
        if not self.tracker.is_available(provider):
            rem = self.tracker.get_remaining_cooldown(provider)
            logger.debug(f"{provider} in backoff ({rem:.0f}s remaining). Skipping.")
            return []

        prg.print_search_start(provider, query)
        try:
            resp = await client.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query},
                headers=DEFAULT_SEARCH_HEADERS,
                timeout=httpx.Timeout(connect=1.5, read=2.5, write=1.5, pool=1.5),
            )
            if resp.status_code == 429:
                prg.print_search_rate_limited(provider, 429)
                self.tracker.mark_blocked(provider, "HTTP 429", duration_seconds=60.0)
                return []
            if resp.status_code in (403, 503) or any(
                sig in resp.text[:10000].lower() for sig in CAPTCHA_SIGNATURES
            ):
                prg.print_search_captcha(provider)
                self.tracker.mark_blocked(provider, "CAPTCHA/Bot detection", duration_seconds=90.0)
                return []
            if resp.status_code != 200:
                prg.print_search_error(provider, f"HTTP {resp.status_code}")
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

            clean_links = list(dict.fromkeys(links))
            if clean_links:
                self.tracker.mark_success(provider)
                prg.print_search_success(provider, len(clean_links))
            else:
                prg.print_search_empty(provider)
            return clean_links
        except (httpx.TimeoutException, TimeoutError):
            prg.print_search_timeout(provider)
            return []
        except httpx.RequestError as exc:
            prg.print_search_error(provider, str(exc))
            return []
        except Exception as exc:
            logger.debug(f"DDG Lite search error: {exc}")
            prg.print_search_error(provider, str(exc))
            return []

    async def _search_ddg_html(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search DuckDuckGo HTML endpoint with fast 2.5s timeout."""
        provider = "DuckDuckGo HTML"
        if not self.tracker.is_available(provider):
            rem = self.tracker.get_remaining_cooldown(provider)
            logger.debug(f"{provider} in backoff ({rem:.0f}s remaining). Skipping.")
            return []

        prg.print_search_start(provider, query)
        try:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers=DEFAULT_SEARCH_HEADERS,
                timeout=httpx.Timeout(connect=1.5, read=2.5, write=1.5, pool=1.5),
            )
            if resp.status_code == 429:
                prg.print_search_rate_limited(provider, 429)
                self.tracker.mark_blocked(provider, "HTTP 429", duration_seconds=60.0)
                return []
            if resp.status_code in (403, 503) or any(
                sig in resp.text[:10000].lower() for sig in CAPTCHA_SIGNATURES
            ):
                prg.print_search_captcha(provider)
                self.tracker.mark_blocked(provider, "CAPTCHA/Bot detection", duration_seconds=90.0)
                return []
            if resp.status_code != 200:
                prg.print_search_error(provider, f"HTTP {resp.status_code}")
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

            clean_links = list(dict.fromkeys(links))
            if clean_links:
                self.tracker.mark_success(provider)
                prg.print_search_success(provider, len(clean_links))
            else:
                prg.print_search_empty(provider)
            return clean_links
        except (httpx.TimeoutException, TimeoutError):
            prg.print_search_timeout(provider)
            return []
        except httpx.RequestError as exc:
            prg.print_search_error(provider, str(exc))
            return []
        except Exception as exc:
            logger.debug(f"DDG HTML search error: {exc}")
            prg.print_search_error(provider, str(exc))
            return []

    async def _search_yahoo(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search Yahoo search with fast 2.5s timeout."""
        provider = "Yahoo Search"
        if not self.tracker.is_available(provider):
            rem = self.tracker.get_remaining_cooldown(provider)
            logger.debug(f"{provider} in backoff ({rem:.0f}s remaining). Skipping.")
            return []

        prg.print_search_start(provider, query)
        try:
            resp = await client.get(
                "https://search.yahoo.com/search",
                params={"p": query},
                headers=DEFAULT_SEARCH_HEADERS,
                timeout=httpx.Timeout(connect=1.5, read=2.5, write=1.5, pool=1.5),
            )
            if resp.status_code == 429:
                prg.print_search_rate_limited(provider, 429)
                self.tracker.mark_blocked(provider, "HTTP 429", duration_seconds=60.0)
                return []
            if resp.status_code in (403, 503) or any(
                sig in resp.text[:10000].lower() for sig in CAPTCHA_SIGNATURES
            ):
                prg.print_search_captcha(provider)
                self.tracker.mark_blocked(provider, "CAPTCHA/Bot detection", duration_seconds=90.0)
                return []
            if resp.status_code != 200:
                prg.print_search_error(provider, f"HTTP {resp.status_code}")
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

            clean_links = list(dict.fromkeys(links))
            if clean_links:
                self.tracker.mark_success(provider)
                prg.print_search_success(provider, len(clean_links))
            else:
                prg.print_search_empty(provider)
            return clean_links
        except (httpx.TimeoutException, TimeoutError):
            prg.print_search_timeout(provider)
            return []
        except httpx.RequestError as exc:
            prg.print_search_error(provider, str(exc))
            return []
        except Exception as exc:
            logger.debug(f"Yahoo search error: {exc}")
            prg.print_search_error(provider, str(exc))
            return []

    async def _search_bing(self, client: httpx.AsyncClient, query: str) -> list[str]:
        """Search Bing organic results with fast 2.5s timeout."""
        provider = "Bing"
        if not self.tracker.is_available(provider):
            rem = self.tracker.get_remaining_cooldown(provider)
            logger.debug(f"{provider} in backoff ({rem:.0f}s remaining). Skipping.")
            return []

        prg.print_search_start(provider, query)
        try:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": query},
                headers=DEFAULT_SEARCH_HEADERS,
                timeout=httpx.Timeout(connect=1.5, read=2.5, write=1.5, pool=1.5),
            )
            if resp.status_code == 429:
                prg.print_search_rate_limited(provider, 429)
                self.tracker.mark_blocked(provider, "HTTP 429", duration_seconds=60.0)
                return []
            if resp.status_code in (403, 503) or any(
                sig in resp.text[:10000].lower() for sig in CAPTCHA_SIGNATURES
            ):
                prg.print_search_captcha(provider)
                self.tracker.mark_blocked(provider, "CAPTCHA/Bot detection", duration_seconds=90.0)
                return []
            if resp.status_code != 200:
                prg.print_search_error(provider, f"HTTP {resp.status_code}")
                return []

            links: list[str] = []
            for href in re.findall(r'href=["\']([^"\']+)["\']', resp.text):
                if "/ck/a" in href:
                    decoded = self._decode_bing_ck(href)
                    if decoded and not self._is_blocked_domain(extract_root_domain(decoded)):
                        links.append(decoded)
                elif href.startswith("http") and not any(
                    b in href for b in ["bing.com", "microsoft.com", "msn.com", "live.com"]
                ):
                    if not self._is_blocked_domain(extract_root_domain(href)):
                        links.append(href)

            clean_links = list(dict.fromkeys(links))
            if clean_links:
                self.tracker.mark_success(provider)
                prg.print_search_success(provider, len(clean_links))
            else:
                prg.print_search_empty(provider)
            return clean_links
        except (httpx.TimeoutException, TimeoutError):
            prg.print_search_timeout(provider)
            return []
        except httpx.RequestError as exc:
            prg.print_search_error(provider, str(exc))
            return []
        except Exception as exc:
            logger.debug(f"Bing search error: {exc}")
            prg.print_search_error(provider, str(exc))
            return []

    def _decode_bing_ck(self, url: str) -> str | None:
        """Decode base64 target url from Bing /ck/a click redirect link."""
        import base64
        import html as html_module

        unescaped = html_module.unescape(url)
        match = re.search(r"[?&]u=a1([a-zA-Z0-9_\-]+)", unescaped)
        if not match:
            return None
        raw_b64 = match.group(1)
        padded = raw_b64 + "=" * (-len(raw_b64) % 4)
        try:
            decoded_bytes = base64.urlsafe_b64decode(padded)
            decoded_url = decoded_bytes.decode("utf-8", errors="ignore")
            if decoded_url.startswith("http"):
                return decoded_url
        except Exception:
            pass
        return None

    async def _extract_directory_website(
        self, client: httpx.AsyncClient, directory_url: str
    ) -> str | None:
        """Inspect a business directory listing with 3.0s timeout to find the company's official outbound website."""
        try:
            resp = await client.get(
                directory_url,
                headers=DEFAULT_SEARCH_HEADERS,
                timeout=httpx.Timeout(connect=1.5, read=2.5, write=1.5, pool=1.5),
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
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        category: str = "roofing",
        street_address: str = "",
        zip_code: str = "",
    ) -> list[str]:
        """Synthesize high-probability domain candidates based on entity identity, trade, and location."""
        stop_words = {
            "llc",
            "inc",
            "corp",
            "corporation",
            "co",
            "company",
            "pa",
            "ltd",
            "limited",
            "pllc",
            "services",
            "service",
            "group",
            "enterprises",
            "enterprise",
            "solutions",
            "solution",
            "fl",
            "florida",
            "contracting",
            "contractors",
            "contractor",
        }
        clean = clean_company_name(company_name).lower()
        raw_tokens = [w for w in re.split(r"[^a-z0-9]", clean) if len(w) >= 2]
        clean_tokens = [w for w in raw_tokens if w not in stop_words]
        if not clean_tokens:
            clean_tokens = raw_tokens

        c_clean = re.sub(r"[^a-z0-9]", "", city.lower())
        st_clean = state.lower().strip()
        cat_clean = category.lower().replace(" ", "").replace("-", "")

        stems: list[str] = []
        if clean_tokens:
            stems.append("".join(clean_tokens))
            stems.append("-".join(clean_tokens))
            if len(clean_tokens) >= 2:
                stems.append("".join(clean_tokens[:2]))

        if raw_tokens and raw_tokens != clean_tokens:
            stems.append("".join(raw_tokens))
            stems.append("-".join(raw_tokens))

        if clean_tokens:
            brand = clean_tokens[0]
            if len(brand) >= 3 and cat_clean not in brand:
                stems.append(f"{brand}{cat_clean}")
                if cat_clean == "roofing":
                    stems.append(f"{brand}roofs")
                    stems.append(f"{brand}pros")
                elif cat_clean in ("pestcontrol", "pest"):
                    stems.append(f"{brand}pest")
                    stems.append(f"{brand}pestcontrol")
                    stems.append(f"{brand}exterminating")

        unique_stems = list(dict.fromkeys([s for s in stems if len(s) >= 3]))
        candidates: list[str] = []

        for stem in unique_stems:
            candidates.append(f"{stem}.com")
            candidates.append(f"{stem}{st_clean}.com")
            candidates.append(f"{stem}florida.com")
            candidates.append(f"{st_clean}{stem}.com")
            if c_clean and len(c_clean) <= 12:
                candidates.append(f"{stem}{c_clean}.com")
                candidates.append(f"{c_clean}{stem}.com")
            if stem == unique_stems[0]:
                candidates.append(f"{stem}.net")
                candidates.append(f"{stem}{st_clean}.net")
                candidates.append(f"{stem}.org")

        return list(dict.fromkeys(candidates))[:28]

    async def _probe_dns(self, domain: str) -> bool:
        """Perform high-speed asynchronous DNS resolution check with 1.0s timeout."""
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.getaddrinfo(domain, 80, family=socket.AF_INET),
                timeout=1.0,
            )
            return True
        except Exception:
            return False

    async def _probe_domain_live(
        self, domain: str, client: httpx.AsyncClient
    ) -> str | None:
        """Check DNS and HTTP availability while filtering out parked/for-sale landers."""
        if not await self._probe_dns(domain):
            return None
        for proto in ["https", "http"]:
            url = f"{proto}://{domain}"
            try:
                resp = await client.get(
                    url,
                    timeout=httpx.Timeout(connect=1.5, read=2.0, write=1.5, pool=1.5),
                )
                if resp.status_code in (200, 301, 302, 307, 308):
                    text_snippet = resp.text[:12000].lower()
                    if any(sig in text_snippet for sig in PARKED_DOMAIN_SIGNATURES):
                        return None
                    return url
            except Exception:
                continue
        return None

    async def find_websites(
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        category: str = "roofing",
        street_address: str = "",
        zip_code: str = "",
    ) -> list[WebsiteCandidate]:
        """Search web indices and synthetic probes for official candidate websites with hard 18s ceiling."""
        timeout_val = min(18.0, max(8.0, self.timeout_seconds * 1.2))
        try:
            return await asyncio.wait_for(
                self._find_websites_inner(
                    company_name=company_name,
                    city=city,
                    state=state,
                    category=category,
                    street_address=street_address,
                    zip_code=zip_code,
                ),
                timeout=timeout_val,
            )
        except TimeoutError:
            logger.debug(
                f"Website discovery timed out for '{company_name}' ({city}, {state}) after {timeout_val}s."
            )
            return []
        except Exception as exc:
            logger.debug(f"Website discovery error for '{company_name}': {exc}")
            return []

    async def _find_websites_inner(
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        category: str = "roofing",
        street_address: str = "",
        zip_code: str = "",
    ) -> list[WebsiteCandidate]:
        """Inner search implementation across cache, APIs, public search, and synthetic probes."""
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
        if street_address:
            queries.append(f'"{clean_name}" {street_address} {city}')

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=1.5, read=3.0, write=1.5, pool=1.5),
            follow_redirects=True,
            verify=False,
            headers=DEFAULT_SEARCH_HEADERS,
        ) as client:
            # 2. Fast Concurrent Synthetic Domain Probing
            synthetic_domains = self._generate_synthetic_domains(
                company_name=clean_name,
                city=city,
                state=state,
                category=category,
                street_address=street_address,
                zip_code=zip_code,
            )
            if synthetic_domains:
                probe_tasks = [self._probe_domain_live(dom, client) for dom in synthetic_domains]
                probed_results = await asyncio.gather(*probe_tasks)
                found_count = len([p for p in probed_results if p])
                prg.print_synthetic_probe_result(found_count, len(synthetic_domains))
                for p_url in probed_results:
                    if not p_url:
                        continue
                    dom = extract_root_domain(p_url)
                    if dom and dom not in seen_domains and not self._is_blocked_domain(dom):
                        seen_domains.add(dom)
                        relevance = self._calculate_domain_relevance(dom, clean_name)
                        candidates.append(
                            WebsiteCandidate(
                                url=f"https://{dom}",
                                domain=dom,
                                discovery_source="synthetic_probe",
                                initial_confidence=max(0.85, relevance),
                            )
                        )
                        if len(candidates) >= 3:
                            break

            # 3. Commercial Search APIs (if keys provided and need more candidates)
            if len(candidates) < 2:
                for query in queries[:2]:
                    api_links = await self._search_commercial_api(client, query)
                    for link in api_links:
                        dom = extract_root_domain(link)
                        if not dom or dom in seen_domains:
                            continue
                        if self._is_blocked_domain(dom):
                            if any(ext in dom for ext in EXTRACTABLE_DIRECTORIES):
                                directory_urls.append(link)
                            continue
                        relevance = self._calculate_domain_relevance(dom, clean_name)
                        if relevance < 0.40:
                            continue
                        seen_domains.add(dom)
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

            # 4. Public Web Search (Bing, DDG, Yahoo)
            if len(candidates) < 2:
                for query in queries[:2]:
                    # 4a. Bing Organic Search
                    search_links = await self._search_bing(client, query)
                    # 4b. DuckDuckGo Lite fallback
                    if not search_links:
                        search_links = await self._search_ddg_lite(client, query)
                    # 4c. DuckDuckGo HTML fallback
                    if not search_links:
                        search_links = await self._search_ddg_html(client, query)
                    # 4d. Yahoo Search fallback
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

                        relevance = self._calculate_domain_relevance(dom, clean_name)
                        if relevance < 0.40:
                            continue
                        seen_domains.add(dom)
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

            # 5. Directory Outbound Link Extraction (if only directory pages were returned)
            if len(candidates) < 2 and directory_urls:
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
