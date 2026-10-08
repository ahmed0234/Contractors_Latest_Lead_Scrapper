"""Deterministic deduplication routines and canonical fingerprint generation."""

import hashlib
import re

from contractor_scraper.discovery.website_finder import extract_root_domain
from contractor_scraper.models.lead import FinalLead
from contractor_scraper.normalization.cleaners import (
    clean_city_name,
    clean_company_name,
    clean_state_code,
)


def generate_company_fingerprint(name: str, city: str, state: str) -> str:
    """Generates a deterministic hash fingerprint for deduplicating companies across data sources.

    Takes normalized company name, city, and state, strips non-alphanumeric noise,
    and computes a SHA-256 fingerprint.
    """
    clean_name = clean_company_name(name)
    c_state = clean_state_code(state) or state.strip().upper()
    c_city = clean_city_name(city) or city.strip().upper()

    # Create simplified alphanumeric token string
    slug_name = re.sub(r"[^a-z0-9]", "", clean_name.lower())
    slug_city = re.sub(r"[^a-z0-9]", "", c_city.lower())
    slug_state = c_state.lower()

    canonical_key = f"{slug_name}:{slug_city}:{slug_state}"
    return hashlib.sha256(canonical_key.encode("utf-8")).hexdigest()


class LeadDeduplicator:
    """Multi-signal deduplicator for verified leads.

    Consolidates duplicates across:
    1. Canonical company fingerprint (name + city + state)
    2. Official website domain
    3. Primary business email
    """

    def __init__(self) -> None:
        self._seen_fingerprints: set[str] = set()
        self._seen_domains: set[str] = set()
        self._seen_emails: set[str] = set()

    def is_duplicate(self, lead: FinalLead) -> bool:
        """Evaluate if lead was already registered under any primary identity key."""
        fp = generate_company_fingerprint(lead.company_name, lead.city, lead.state)
        domain = extract_root_domain(lead.website)
        email = lead.email.lower().strip()

        if fp in self._seen_fingerprints:
            return True
        if domain and domain in self._seen_domains:
            return True
        if email and email in self._seen_emails:
            return True

        return False

    def register(self, lead: FinalLead) -> None:
        """Register lead's identity markers into deduplication sets."""
        fp = generate_company_fingerprint(lead.company_name, lead.city, lead.state)
        domain = extract_root_domain(lead.website)
        email = lead.email.lower().strip()

        self._seen_fingerprints.add(fp)
        if domain:
            self._seen_domains.add(domain)
        if email:
            self._seen_emails.add(email)

    def deduplicate_list(self, leads: list[FinalLead]) -> list[FinalLead]:
        """Deduplicate an entire list of leads in-order, keeping first occurrence."""
        unique_leads: list[FinalLead] = []
        for lead in leads:
            if not self.is_duplicate(lead):
                self.register(lead)
                unique_leads.append(lead)
        return unique_leads
