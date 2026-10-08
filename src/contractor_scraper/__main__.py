"""Execution entrypoint when invoking `python -m contractor_scraper`."""

from contractor_scraper.cli.app import app

if __name__ == "__main__":
    app()
