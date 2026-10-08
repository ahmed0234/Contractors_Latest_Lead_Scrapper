"""Project entrypoint allowing `python -m app` execution."""

import sys
from pathlib import Path

# Automatically ensure src/ is on Python search path
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from contractor_scraper.cli.app import app  # noqa: E402

if __name__ == "__main__":
    app()
