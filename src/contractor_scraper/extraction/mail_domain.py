"""Mail-domain sanity checks for publicly listed business emails.

Looks up MX (falling back to A) records over DNS-over-HTTPS so no extra DNS dependency is needed.
A passing check only shows the domain can receive mail - it is never proof that a mailbox exists.
"""

import asyncio

import httpx

from contractor_scraper.core.logging import get_logger

logger = get_logger("mail_domain")

DOH_ENDPOINTS: tuple[str, ...] = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

FREE_MAIL_PROVIDERS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "comcast.net", "att.net", "sbcglobal.net",
    "bellsouth.net", "verizon.net", "cox.net", "charter.net", "earthlink.net", "protonmail.com", "proton.me",
})

MAIL_OK = "mx_ok"
MAIL_A_ONLY = "a_record_only"
MAIL_FREE_PROVIDER = "free_provider"
MAIL_NO_HOST = "no_mail_host"
MAIL_UNCHECKED = "unchecked"


class MailDomainChecker:
    """Caches per-domain MX/A lookups; network problems yield 'unchecked', never a rejection."""

    def __init__(self, timeout_seconds: float = 6.0, enabled: bool = True) -> None:
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled
        self._cache: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _query(self, client: httpx.AsyncClient, domain: str, rtype: str) -> tuple[int, int] | None:
        """Return (dns_status, answer_count) or None when no resolver answered."""
        for endpoint in DOH_ENDPOINTS:
            try:
                resp = await client.get(
                    endpoint,
                    params={"name": domain, "type": rtype},
                    headers={"Accept": "application/dns-json"},
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                wanted = 15 if rtype == "MX" else 1
                answers = [a for a in data.get("Answer", []) if a.get("type") == wanted]
                return int(data.get("Status", 0)), len(answers)
            except Exception as exc:
                logger.debug(f"DoH {rtype} lookup for {domain} via {endpoint} failed: {exc}")
        return None

    async def check(self, domain: str) -> str:
        domain = (domain or "").lower().strip().rstrip(".")
        if not domain:
            return MAIL_UNCHECKED
        if domain in FREE_MAIL_PROVIDERS:
            return MAIL_FREE_PROVIDER
        if not self.enabled:
            return MAIL_UNCHECKED
        if domain in self._cache:
            return self._cache[domain]
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            if domain in self._cache:
                return self._cache[domain]
            result = MAIL_UNCHECKED
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                mx = await self._query(client, domain, "MX")
                if mx is not None:
                    status, count = mx
                    if status == 3:  # NXDOMAIN
                        result = MAIL_NO_HOST
                    elif count:
                        result = MAIL_OK
                    else:
                        a = await self._query(client, domain, "A")
                        if a is not None:
                            result = MAIL_A_ONLY if a[1] else MAIL_NO_HOST
            if result != MAIL_UNCHECKED:
                self._cache[domain] = result
            return result
