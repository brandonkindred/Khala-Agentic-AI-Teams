"""Postgres schema owned by the unified_api.

Declares the ``encrypted_integration_credentials`` table that
``postgres_encrypted_credentials.py`` and ``google_browser_login_credentials.py``
both read/write, plus the ``llm_provider_configs`` table that holds the ordered
multi-provider LLM fallback list. Registered from the unified_api's FastAPI lifespan.
"""

from __future__ import annotations

from llm_service.provider_store import PROVIDER_TABLE_STATEMENTS
from llm_service.provider_store import TABLE_NAME as PROVIDER_TABLE_NAME
from shared_postgres import TeamSchema
from shared_postgres.secrets import SECRETS_TABLE_DDL

SCHEMA = TeamSchema(
    team="unified_api",
    database=None,  # default POSTGRES_DB
    # Each CREATE TABLE lives once in its owning module (shared_postgres.secrets and
    # llm_service.provider_store), both of which also self-heal the table lazily, so
    # the registered schema and the lazy ensure can't drift.
    statements=[SECRETS_TABLE_DDL, *PROVIDER_TABLE_STATEMENTS],
    table_names=[
        "encrypted_integration_credentials",
        PROVIDER_TABLE_NAME,
    ],
)
