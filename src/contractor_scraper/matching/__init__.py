"""Matching and deduplication module exports."""

from contractor_scraper.matching.base import BaseCompanyMatcher, BaseDeduplicator, MatchResult
from contractor_scraper.matching.dedup import LeadDeduplicator, generate_company_fingerprint

__all__ = [
    "BaseCompanyMatcher",
    "BaseDeduplicator",
    "MatchResult",
    "generate_company_fingerprint",
    "LeadDeduplicator",
]
