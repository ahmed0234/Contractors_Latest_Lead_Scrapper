"""Structured logging infrastructure with rich terminal output and JSON file logging."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler

console = Console()


class JSONFormatter(logging.Formatter):
    """Formats log records as structured JSON lines for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include structured contextual attributes if attached to record
        for attr in (
            "stage",
            "run_id",
            "company_id",
            "processed_count",
            "total_count",
            "retries",
            "skipped_count",
            "duration_ms",
            "error_type",
            "is_retryable",
        ):
            if hasattr(record, attr):
                payload[attr] = getattr(record, attr)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class StageLoggerAdapter(logging.LoggerAdapter):
    """Context-aware logger adapter that automatically injects pipeline stage & run metadata."""

    def __init__(
        self,
        logger: logging.Logger,
        stage: str | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(logger, {"stage": stage, "run_id": run_id})

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        extra = kwargs.setdefault("extra", {})
        if self.extra:
            for k, v in self.extra.items():
                if v is not None and k not in extra:
                    extra[k] = v
        return msg, kwargs

    def record_progress(
        self,
        message: str,
        processed: int,
        total: int | None = None,
        skipped: int = 0,
        duration_ms: float | None = None,
    ) -> None:
        """Helper to log batch progress metrics in a structured manner."""
        extra: dict[str, Any] = {
            "processed_count": processed,
            "skipped_count": skipped,
        }
        if total is not None:
            extra["total_count"] = total
        if duration_ms is not None:
            extra["duration_ms"] = round(duration_ms, 2)

        self.info(message, extra=extra)


def setup_logging(
    log_level: str = "INFO",
    logs_dir: Path | None = None,
    json_logging: bool = True,
    log_file_name: str = "contractor_scraper.log",
) -> None:
    """Configures application-wide logging handlers.

    - Console: RichHandler with syntax highlighting and timestamps
    - File: Rotating or structured JSON line logging
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Avoid duplicate handlers if setup_logging is called multiple times
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    # 1. Rich Console Handler
    console_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )
    console_handler.setLevel(numeric_level)
    console_formatter = logging.Formatter(
        fmt="[%(name)s] %(message)s",
        datefmt="[%X]",
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 2. File Handler (Structured JSON or standard text)
    if logs_dir:
        logs_dir = Path(logs_dir)
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_path = logs_dir / log_file_name

        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setLevel(numeric_level)
        if json_logging:
            file_handler.setFormatter(JSONFormatter())
        else:
            file_handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        root_logger.addHandler(file_handler)


def get_logger(
    name: str,
    stage: str | None = None,
    run_id: str | None = None,
) -> StageLoggerAdapter:
    """Convenience factory to retrieve a contextual logger."""
    base_logger = logging.getLogger(name)
    return StageLoggerAdapter(base_logger, stage=stage, run_id=run_id)
