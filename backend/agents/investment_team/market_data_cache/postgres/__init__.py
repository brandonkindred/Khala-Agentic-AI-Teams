"""Postgres schema for the investment team's market-data cache (issue #376).

Pure data — importing this module has no side effects.  DDL is applied
when ``register_team_schemas(SCHEMA)`` is called from a FastAPI lifespan.

Single table, ``investment_market_data_snapshots``, indexes the
content-addressed Parquet snapshots written under
``${AGENT_CACHE}/investment_team/market_data/...``.  Each row is a
``(symbol, asset_class, frequency, provider, fetch_ts)`` snapshot whose
range is ``[start_date, end_date]`` and whose canonical SHA256 is
``sha256``.  The ``parquet_path`` column stores the absolute path to the
on-disk artifact; the cache layer treats a missing file as a stale row
and re-fetches.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="investment_market_data",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS investment_market_data_snapshots (
            id              BIGSERIAL PRIMARY KEY,
            symbol          TEXT NOT NULL,
            asset_class     TEXT NOT NULL,
            frequency       TEXT NOT NULL,
            provider        TEXT NOT NULL,
            fetch_ts        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            start_date      DATE NOT NULL,
            end_date        DATE NOT NULL,
            row_count       INTEGER NOT NULL,
            sha256          TEXT NOT NULL,
            schema_version  INTEGER NOT NULL DEFAULT 1,
            parquet_path    TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_imds_lookup
            ON investment_market_data_snapshots
            (symbol, asset_class, frequency, fetch_ts DESC)""",
        """CREATE INDEX IF NOT EXISTS idx_imds_sha
            ON investment_market_data_snapshots (sha256)""",
    ],
    table_names=["investment_market_data_snapshots"],
)

__all__ = ["SCHEMA"]
