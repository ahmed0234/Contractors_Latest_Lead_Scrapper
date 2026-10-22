"""Website discovery tuned for Christmas / holiday lighting installers found in Sunbiz filings.

Reuses the shared `WebsiteFinder` (synthetic domain probes -> optional commercial search APIs ->
Bing -> DuckDuckGo -> Yahoo -> directory outbound links) and adds:
- niche-aware search queries ("<name>" christmas light installation <city>),
- holiday-lighting domain stems for synthetic probing (<brand>holidaylights.com, ...),
- a shared search gate that spaces out public search requests across concurrent workers,
- a separate discovery cache so other categories' cached candidates are never reused here,
- `SearchOutcome.search_responded`, telling "nothing found" apart from "search was unavailable".
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from contractor_scraper.core.logging import get_logger
from contractor_scraper.discovery.base import WebsiteCandidate
from contractor_scraper.discovery.website_finder import (
    SearchCallStats,
    WebsiteFinder,
    _search_call_stats,
)
from contractor_scraper.normalization.cleaners import clean_company_name

logger = get_logger("christmas_website_finder")

NICHE_NAME_PATTERN = re.compile(
    r"\b(christmas|xmas|holiday|holidays|lights|lighting|lites|illuminations?|trimlight)\b",
    re.IGNORECASE,
)


@dataclass
class SearchOutcome:
    """Website candidates for one company plus whether any search provider actually answered."""

    candidates: list[WebsiteCandidate] = field(default_factory=list)
    search_responded: bool = False


class SearchGate:
    """Serializes public search requests and enforces a minimum spacing between them."""

    def __init__(self, min_interval: float = 1.5, max_parallel: int = 1) -> None:
        self.min_interval = max(0.0, min_interval)
        self._semaphore = asyncio.Semaphore(max(1, max_parallel))
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    async def __aenter__(self) -> "SearchGate":
        await self._semaphore.acquire()
        async with self._lock:
            wait = self._next_slot - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_slot = time.monotonic() + self.min_interval
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._semaphore.release()


class ChristmasWebsiteFinder(WebsiteFinder):
    """Finds official website candidates for Sunbiz companies that may install holiday lighting."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        timeout_seconds: float = 12.0,
        discovery_timeout: float = 75.0,
        search_interval: float = 1.5,
        search_read_timeout: float = 10.0,
        search_gate: SearchGate | None = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir, timeout_seconds=timeout_seconds)
        if cache_dir is None:
            self.cache_dir = self.settings.cache_dir / "discovery_christmas"
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.discovery_timeout = discovery_timeout
        # Public search pages regularly take longer than the shared finder's 2.5s read budget
        self.search_timeout = httpx.Timeout(connect=5.0, read=search_read_timeout, write=5.0, pool=5.0)
        self.search_gate = search_gate or SearchGate(min_interval=search_interval)

    def _build_search_queries(
        self,
        clean_name: str,
        city: str,
        state: str,
        category: str,
        street_address: str = "",
    ) -> list[str]:
        place = f"{city} {state}".strip() if city else "Florida"
        if NICHE_NAME_PATTERN.search(clean_name):
            # The name already carries the niche; pin it to the registered location
            queries = [f'"{clean_name}" {place}', f'"{clean_name}" Florida']
        else:
            queries = [
                f'"{clean_name}" {place}',
                f'"{clean_name}" christmas light installation {city or "Florida"}',
            ]
        if street_address:
            queries.append(f'"{clean_name}" {street_address} {city}')
        return queries

    def _generate_synthetic_domains(
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        category: str = "lights",
        street_address: str = "",
        zip_code: str = "",
    ) -> list[str]:
        base = super()._generate_synthetic_domains(
            company_name=company_name,
            city=city,
            state=state,
            category="lights",
            street_address=street_address,
            zip_code=zip_code,
        )
        clean = clean_company_name(company_name).lower()
        tokens = [t for t in re.split(r"[^a-z0-9]", clean) if len(t) >= 2]
        brand_tokens = [t for t in tokens if not NICHE_NAME_PATTERN.fullmatch(t)]
        extra: list[str] = []
        if brand_tokens and brand_tokens != tokens:
            brand = "".join(brand_tokens)
            if len(brand) >= 3:
                for suffix in ("holidaylights", "christmaslights", "lighting", "holidaylighting"):
                    extra.append(f"{brand}{suffix}.com")
        return list(dict.fromkeys(base + extra))[:32]

    async def _search_bing(self, client: httpx.AsyncClient, query: str) -> list[str]:
        async with self.search_gate:
            return await super()._search_bing(client, query)

    async def _search_ddg_lite(self, client: httpx.AsyncClient, query: str) -> list[str]:
        async with self.search_gate:
            return await super()._search_ddg_lite(client, query)

    async def _search_ddg_html(self, client: httpx.AsyncClient, query: str) -> list[str]:
        async with self.search_gate:
            return await super()._search_ddg_html(client, query)

    async def _search_yahoo(self, client: httpx.AsyncClient, query: str) -> list[str]:
        async with self.search_gate:
            return await super()._search_yahoo(client, query)

    async def find_websites(
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        category: str = "christmas lights",
        street_address: str = "",
        zip_code: str = "",
    ) -> list[WebsiteCandidate]:
        return (
            await self.discover(company_name, city, state, street_address, zip_code)
        ).candidates

    async def discover(
        self,
        company_name: str,
        city: str,
        state: str = "FL",
        street_address: str = "",
        zip_code: str = "",
    ) -> SearchOutcome:
        """Find candidate websites; `search_responded` is False when no provider answered at all."""
        cache_file = self._get_cache_path(company_name, city, state)
        cached = cache_file.exists()
        stats = SearchCallStats()
        token = _search_call_stats.set(stats)
        try:
            candidates = await asyncio.wait_for(
                self._find_websites_inner(
                    company_name=company_name,
                    city=city,
                    state=state,
                    category="christmas lights",
                    street_address=street_address,
                    zip_code=zip_code,
                ),
                timeout=self.discovery_timeout,
            )
        except TimeoutError:
            logger.debug(f"Website discovery timed out for '{company_name}' after {self.discovery_timeout}s")
            candidates = []
        except Exception as exc:
            logger.debug(f"Website discovery error for '{company_name}': {exc}")
            candidates = []
        finally:
            _search_call_stats.reset(token)
        # Candidates from the cache or from synthetic probes are real answers even without a search call
        responded = stats.responses > 0 or bool(candidates) or cached
        return SearchOutcome(candidates=candidates, search_responded=responded)
