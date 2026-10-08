"""Database engine, session management, and initialization for persistent storage."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from contractor_scraper.config.settings import get_settings
from contractor_scraper.core.exceptions import DatabaseError
from contractor_scraper.storage.models import Base


def _enable_sqlite_optimizations(dbapi_connection: Any, connection_record: Any) -> None:
    """Configures high-performance and ACID reliability settings for SQLite.

    Enables Write-Ahead Logging (WAL) and foreign key constraints.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


def create_db_engine(db_path_or_url: str | Path | None = None) -> Engine:
    """Creates a configured SQLAlchemy Engine.

    Supports custom paths, in-memory sqlite, or defaults from Settings.
    """
    settings = get_settings()

    if db_path_or_url is None:
        db_path = settings.database_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path}"
    elif isinstance(db_path_or_url, Path):
        db_path_or_url.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{db_path_or_url}"
    elif db_path_or_url.startswith("sqlite"):
        url = db_path_or_url
    else:
        url = f"sqlite:///{db_path_or_url}"

    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}

    engine = create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )

    if is_sqlite and ":memory:" not in url:
        event.listen(engine, "connect", _enable_sqlite_optimizations)

    return engine


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Retrieve global cached Engine instance."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Retrieve global sessionmaker."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def get_db_session(engine: Engine | None = None) -> Generator[Session, None, None]:
    """Context manager yielding a transactional database session."""
    factory = sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception as exc:
        session.rollback()
        raise DatabaseError(f"Database transaction failed: {exc}") from exc
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Initialize database schema by creating all registered tables."""
    target_engine = engine or get_engine()
    Base.metadata.create_all(bind=target_engine)
