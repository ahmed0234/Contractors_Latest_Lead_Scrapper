"""Florida Department of State / Division of Corporations (Sunbiz) data acquisition layer.

Downloads and streams official public corporate filings from the state's SFTP/HTTPS portal.
"""

import re
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

from contractor_scraper.acquisition.base import BaseStateAcquisitionSource
from contractor_scraper.config.settings import get_settings
from contractor_scraper.core import progress as prg
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
        Returns an empty list (without raising) when no files are found for the requested
        year, so that multi-year callers can cleanly skip to the next year.
        If the remote portal is unreachable but valid local files are cached, returns cached files.
        """
        year_str = str(year)
        local_dir = self.get_raw_storage_dir(year)
        local_files = sorted(
            [f.name for f in local_dir.glob(f"{year_str}*c.txt") if f.stat().st_size > 0]
        )

        logger.info(f"Querying Sunbiz server for {year_str} corporate filing files...")

        for attempt in range(1, 4):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
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
                    all_files = sorted(set(re.findall(r"(\d{8}c\.txt)", text)))
                    year_files = [f for f in all_files if f.startswith(year_str)]

                    if not year_files:
                        if local_files:
                            logger.info(
                                f"No remote filing files found on portal for {year}, but found {len(local_files)} cached files."
                            )
                            return local_files
                        available_years = sorted({f[:4] for f in all_files})
                        logger.info(
                            f"No Sunbiz corporate datasets found for year {year}. "
                            f"Available years on the official portal: {available_years}"
                        )
                        return []

                    logger.info(
                        f"Found {len(year_files)} daily filing files on Sunbiz for year {year_str}."
                    )
                    return year_files

            except (httpx.RequestError, AcquisitionError) as exc:
                if attempt == 3:
                    if local_files:
                        logger.info(
                            f"Sunbiz portal unreachable after 3 attempts; falling back to {len(local_files)} locally cached files for year {year_str}."
                        )
                        return local_files
                    logger.warning(
                        f"Network error reaching Sunbiz portal ({self.base_url}) after 3 attempts: {exc}"
                    )
                    return []
                logger.warning(
                    f"Sunbiz connection attempt {attempt} failed: {exc}. Retrying in {attempt * 2}s..."
                )
                time.sleep(attempt * 2)

        return local_files

    def download_file(
        self,
        file_name: str,
        dest_dir: Path | None = None,
        force_download: bool = False,
    ) -> Path:
        """Download an individual daily filing file from Sunbiz, reusing local cache if present.

        Validates cache against 0-byte files and corrupt HTML error responses.
        """
        year = int(file_name[:4])
        target_dir = dest_dir or self.get_raw_storage_dir(year)
        target_file = target_dir / file_name

        if target_file.exists():
            if target_file.stat().st_size == 0:
                logger.warning(f"Removing empty 0-byte cached file: {target_file}")
                target_file.unlink(missing_ok=True)
            elif not force_download:
                # Inspect first 256 bytes to ensure not a 404/500 HTML error page saved as .txt
                try:
                    with open(target_file, "rb") as check_f:
                        head = check_f.read(256).lower()
                        if b"<!doctype" in head or b"<html" in head:
                            logger.warning(
                                f"Removing corrupted HTML error response from cache: {target_file}"
                            )
                            target_file.unlink(missing_ok=True)
                        else:
                            logger.debug(f"Reusing cached Sunbiz file: {target_file}")
                            prg.print_file_cached(file_name)
                            return target_file
                except Exception:
                    pass

        file_url = f"{self.base_url}{file_name}"
        logger.info(f"Downloading Sunbiz filing file: {file_name} from {file_url}...")
        prg.print_file_download(file_name, year)

        for attempt in range(1, 4):
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0),
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

                logger.info(
                    f"Saved {file_name} to {target_file} ({target_file.stat().st_size} bytes)."
                )
                return target_file

            except (httpx.RequestError, AcquisitionError) as exc:
                if attempt == 3:
                    raise AcquisitionError(
                        f"Failed to download Sunbiz filing file {file_name} after 3 attempts: {exc}"
                    ) from exc
                logger.warning(
                    f"Download attempt {attempt} for {file_name} failed: {exc}. Retrying in {attempt * 2}s..."
                )
                time.sleep(attempt * 2)

        return target_file

    def stream_files_for_year(
        self,
        year: int,
        max_files: int | None = None,
    ) -> Iterator[tuple[str, int, int, list[str]]]:
        """Discover daily filing files for a year and yield (file_name, file_index, total_files, lines).

        Reuses cached files, downloads missing or invalid files, and avoids redownloading.
        """
        year_files = self.list_available_files_for_year(year)
        if max_files:
            year_files = year_files[:max_files]

        total_files = len(year_files)
        for idx, file_name in enumerate(year_files, start=1):
            try:
                file_path = self.download_file(file_name)
                with open(file_path, encoding="latin-1", errors="replace") as f:
                    lines = [line.rstrip("\r\n") for line in f if line.strip()]
                yield file_name, idx, total_files, lines
            except Exception as file_err:
                logger.warning(f"Skipping {file_name} due to acquisition error: {file_err}")
                continue

    def stream_raw_records_for_year(
        self,
        year: int,
        max_files: int | None = None,
    ) -> Iterator[str]:
        """Stream raw 1440-byte record lines for a given year across all daily filing files.

        Downloads files on-demand (or uses cached files), streaming lines one by one
        to avoid loading large files into memory.
        """
        for _, _, _, lines in self.stream_files_for_year(year, max_files=max_files):
            yield from lines

    async def acquire(
        self,
        state: str,
        category: ContractorCategory,
        limit: int | None = None,
    ):
        """Implementation satisfying BaseStateAcquisitionSource interface."""
        raise NotImplementedError("Use stream_raw_records_for_year for targeted year batching.")

    async def download_bulk_archive(self, state: str, destination_dir: Path) -> Path | None:
        """Download quarterly bulk archive if requested."""
        return None
