"""Florida Department of State / Division of Corporations (Sunbiz) data acquisition layer.

Downloads and streams official public corporate filings from the state's SFTP/HTTPS portal.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import httpx

from contractor_scraper.acquisition.base import BaseStateAcquisitionSource
from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.enums import ContractorCategory
from contractor_scraper.core.exceptions import AcquisitionError
from contractor_scraper.core.logging import get_logger

logger = get_logger("sunbiz_acquisition")

SUNBIZ_BASE_URL = "https://sftp.floridados.gov/Public/doc/cor/"
SUNBIZ_AUTH = ("Public", "PubAccess1845!")


class FloridaSunbizSource(BaseStateAcquisitionSource):
    """Authoritative public data acquisition source for Florida Division of Corporations (Sunbiz).

    Connects to Florida DOS secure public data portal, discovers daily corporate filing
    datasets for the requested year, caches downloads locally, and streams raw records.
    """

    def __init__(
        self,
        base_url: str = SUNBIZ_BASE_URL,
        auth: tuple[str, str] = SUNBIZ_AUTH,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.auth = auth
        self.timeout_seconds = timeout_seconds
        self.settings = get_settings()

    @property
    def source_name(self) -> str:
        return "florida_sunbiz"

    @property
    def supported_states(self) -> list[str]:
        return ["FL"]

    def get_raw_storage_dir(self, year: int) -> Path:
        """Local directory where raw Sunbiz data files for this year are cached."""
        target = self.settings.raw_data_dir / "sunbiz" / str(year)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def list_available_files_for_year(self, year: int) -> list[str]:
        """Query Sunbiz directory listing to identify all daily filing files for the given year.

        Filename convention: YYYYMMDDc.txt
        """
        year_str = str(year)
        logger.info(f"Querying Sunbiz server for {year_str} corporate filing files...")

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                auth=self.auth,
                headers={"User-Agent": "python-httpx/0.28.1 (ContractorScraper-DataSync)"},
            ) as client:
                resp = client.get(self.base_url)
                if resp.status_code != 200:
                    raise AcquisitionError(
                        f"Failed to access Sunbiz data portal (HTTP {resp.status_code})."
                    )

                text = resp.text
                # Match filenames like 20250102c.txt
                all_files = sorted(set(re.findall(r"(\d{8}c\.txt)", text)))
                year_files = [f for f in all_files if f.startswith(year_str)]

                if not year_files:
                    # Collect available years to provide helpful diagnostic error
                    available_years = sorted({f[:4] for f in all_files})
                    raise AcquisitionError(
                        f"No official Sunbiz corporate datasets available for year {year}. "
                        f"Available years on official portal are: {available_years}"
                    )

                logger.info(
                    f"Found {len(year_files)} daily filing files on Sunbiz for year {year_str}."
                )
                return year_files

        except httpx.RequestError as exc:
            raise AcquisitionError(
                f"Network error connecting to official Sunbiz portal ({self.base_url}): {exc}"
            ) from exc

    def download_file(
        self,
        file_name: str,
        dest_dir: Path | None = None,
        force_download: bool = False,
    ) -> Path:
        """Download an individual daily filing file from Sunbiz, reusing local cache if present."""
        year = int(file_name[:4])
        target_dir = dest_dir or self.get_raw_storage_dir(year)
        target_file = target_dir / file_name

        if target_file.exists() and target_file.stat().st_size > 0 and not force_download:
            logger.debug(f"Reusing cached Sunbiz file: {target_file}")
            return target_file

        file_url = f"{self.base_url}{file_name}"
        logger.info(f"Downloading Sunbiz filing file: {file_name} from {file_url}...")

        try:
            with httpx.Client(
                timeout=self.timeout_seconds * 2,
                follow_redirects=True,
                auth=self.auth,
                headers={"User-Agent": "python-httpx/0.28.1 (ContractorScraper-DataSync)"},
            ) as client:
                with client.stream("GET", file_url) as stream:
                    if stream.status_code != 200:
                        raise AcquisitionError(
                            f"Sunbiz returned HTTP {stream.status_code} for {file_url}"
                        )
                    temp_file = target_file.with_suffix(".tmp")
                    with open(temp_file, "wb") as f:
                        for chunk in stream.iter_bytes(chunk_size=65536):
                            f.write(chunk)
                    temp_file.replace(target_file)

            logger.info(f"Saved {file_name} to {target_file} ({target_file.stat().st_size} bytes).")
            return target_file

        except httpx.RequestError as exc:
            raise AcquisitionError(
                f"Failed to download Sunbiz filing file {file_name}: {exc}"
            ) from exc

    def stream_raw_records_for_year(
        self,
        year: int,
        max_files: int | None = None,
    ) -> Iterator[str]:
        """Stream raw 1440-byte record lines for a given year across all daily filing files.

        Downloads files on-demand (or uses cached files), streaming lines one by one
        to avoid loading large files into memory.
        """
        year_files = self.list_available_files_for_year(year)
        if max_files:
            year_files = year_files[:max_files]

        for file_name in year_files:
            file_path = self.download_file(file_name)
            with open(file_path, encoding="latin-1", errors="replace") as f:
                for line in f:
                    stripped = line.rstrip("\r\n")
                    if stripped:
                        yield stripped

    async def acquire(
        self,
        state: str,
        category: ContractorCategory,
        limit: int | None = None,
    ):
        """Implementation satisfying BaseStateAcquisitionSource interface."""
        # Yields from default year (current or 2025)
        raise NotImplementedError("Use stream_raw_records_for_year for targeted year batching.")

    async def download_bulk_archive(self, state: str, destination_dir: Path) -> Path | None:
        """Download quarterly bulk archive if requested."""
        return None
