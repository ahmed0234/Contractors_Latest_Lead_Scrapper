"""High-performance analytical engine using DuckDB and Polars integration."""

from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from contractor_scraper.config.settings import get_settings


class AnalyticsEngine:
    """Analytical data engine using DuckDB and Polars for large-scale tabular processing.

    Provides high-speed analytical queries on raw state archives, SQLite DB attachments,
    and fast Parquet / Arrow data frame export.
    """

    def __init__(self, duckdb_path: Path | None = None) -> None:
        settings = get_settings()
        self.db_path = duckdb_path or settings.duckdb_path
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> duckdb.DuckDBPyConnection:
        """Open or connect to DuckDB database instance."""
        return duckdb.connect(str(self.db_path))

    def query_to_polars(self, sql_query: str, params: list[Any] | None = None) -> pl.DataFrame:
        """Execute a SQL query in DuckDB and return the result zero-copy as a Polars DataFrame."""
        with self.get_connection() as conn:
            arrow_table = conn.execute(sql_query, params or []).arrow()
            return pl.from_arrow(arrow_table)

    def attach_sqlite_database(self, sqlite_path: Path | None = None) -> None:
        """Attach SQLite database into DuckDB session for direct cross-database analytical queries."""
        settings = get_settings()
        target_path = sqlite_path or settings.database_path
        if not target_path.exists():
            return

        with self.get_connection() as conn:
            # Install & load sqlite extension if needed, then attach
            conn.execute(f"ATTACH '{target_path}' AS sqlite_store (TYPE SQLITE);")

    def export_query_to_parquet(self, sql_query: str, output_parquet_path: Path) -> None:
        """Directly stream analytical query output into a Parquet file without high memory overhead."""
        output_parquet_path.parent.mkdir(parents=True, exist_ok=True)
        with self.get_connection() as conn:
            conn.execute(
                f"COPY ({sql_query}) TO '{output_parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD);"
            )
