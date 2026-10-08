"""Standalone maintenance script to initialize database tables."""

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.logging import setup_logging
from contractor_scraper.storage.database import get_engine, init_db
from contractor_scraper.storage.models import Base


def main() -> None:
    settings = get_settings()
    settings.ensure_directories()
    setup_logging(log_level=settings.log_level, logs_dir=settings.logs_dir)

    print(f"Connecting to database at {settings.database_path}...")
    engine = get_engine()
    init_db(engine)

    tables = list(Base.metadata.tables.keys())
    print(f"Initialization successful! Created {len(tables)} tables:")
    for t in tables:
        print(f" - {t}")


if __name__ == "__main__":
    main()
