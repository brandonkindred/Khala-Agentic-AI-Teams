"""Postgres schema for the sales team.

Pure-data declaration — no side effects on import. The team's FastAPI
lifespan calls ``register_team_schemas(SCHEMA)`` at startup. Tables are
prefixed with ``sales_`` to avoid collisions in the shared ``POSTGRES_DB``.
"""

from __future__ import annotations

from shared.postgres import TeamSchema

SCHEMA = TeamSchema(
    team="sales",
    database=None,  # default POSTGRES_DB
    statements=[
        # dossier_store.py — per-prospect deep-research dossiers
        """CREATE TABLE IF NOT EXISTS sales_dossiers (
            id            TEXT PRIMARY KEY,
            prospect_id   TEXT NOT NULL,
            company_name  TEXT NOT NULL,
            full_name     TEXT NOT NULL,
            data          JSONB NOT NULL,
            generated_at  TIMESTAMPTZ NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sales_dossiers_company ON sales_dossiers(company_name)",
        "CREATE INDEX IF NOT EXISTS idx_sales_dossiers_prospect ON sales_dossiers(prospect_id)",
        # dossier_store.py — ranked deep-research prospect lists
        """CREATE TABLE IF NOT EXISTS sales_prospect_lists (
            id                     TEXT PRIMARY KEY,
            product_name           TEXT NOT NULL,
            total_prospects        INTEGER NOT NULL,
            companies_represented  INTEGER NOT NULL,
            data                   JSONB NOT NULL,
            generated_at           TIMESTAMPTZ NOT NULL,
            created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_sales_prospect_lists_product
            ON sales_prospect_lists(product_name, generated_at DESC)""",
    ],
    table_names=[
        "sales_dossiers",
        "sales_prospect_lists",
    ],
)
