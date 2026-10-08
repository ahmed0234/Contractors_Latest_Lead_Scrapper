"""Abstract interfaces for acquiring public state business registers and licensing data."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contractor_scraper.core.enums import ContractorCategory


@dataclass
class RawAcquisitionPayload:
    """Container for raw public state or registry record data."""

    source_name: str
    source_record_id: str
    state: str
    raw_content: dict[str, Any]
    acquired_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseStateAcquisitionSource(ABC):
    """Abstract base class for state-specific public company data collectors.

    Subclasses implement acquisition logic for specific public portals (e.g. SOS filings,
    state contractor licensing boards like CSLB, TDLR, etc.) without commercial APIs.
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Unique identifier for this public data source (e.g. 'tx_sos', 'ca_cslb')."""
        pass

    @property
    @abstractmethod
    def supported_states(self) -> list[str]:
        """List of 2-letter US state postal codes supported by this source."""
        pass

    @abstractmethod
    async def acquire(
        self,
        state: str,
        category: ContractorCategory,
        limit: int | None = None,
    ) -> AsyncIterator[RawAcquisitionPayload]:
        """Stream raw entity records acquired from public registers for a target state and category."""
        ...

    @abstractmethod
    async def download_bulk_archive(self, state: str, destination_dir: Path) -> Path | None:
        """Download public bulk dataset archive if provided by the state agency."""
        ...
