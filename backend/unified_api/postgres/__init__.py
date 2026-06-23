"""Postgres schema owned by the unified_api.

Declares the single ``encrypted_integration_credentials`` table that
``postgres_encrypted_credentials.py`` and ``google_browser_login_credentials.py``
both read/write. Registered from the unified_api's FastAPI lifespan.
"""

from __future__ import annotations

from shared_postgres import TeamSchema
from shared_postgres.secrets import SECRETS_TABLE_DDL

SCHEMA = TeamSchema(
    team="unified_api",
    database=None,  # default POSTGRES_DB
    # The CREATE TABLE lives once in shared_postgres.secrets (the shared store that
    # also self-heals it), so the registered schema and the lazy ensure can't drift.
    statements=[SECRETS_TABLE_DDL],
    table_names=[
        "encrypted_integration_credentials",
    ],
)
