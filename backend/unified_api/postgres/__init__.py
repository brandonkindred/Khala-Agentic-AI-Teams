"""Postgres schema owned by the unified_api.

Declares the ``encrypted_integration_credentials`` table that
``postgres_encrypted_credentials.py`` and ``google_browser_login_credentials.py``
both read/write, plus the ``llm_provider_configs`` table that holds the ordered
multi-provider LLM fallback list, plus ``llm_call_records`` for platform-wide
token-usage persistence. Registered from the unified_api's FastAPI lifespan.
"""

from __future__ import annotations

from llm_service.provider_store import PROVIDER_TABLE_STATEMENTS
from llm_service.provider_store import TABLE_NAME as PROVIDER_TABLE_NAME
from llm_service.usage_store import TABLE_NAME as USAGE_TABLE_NAME
from llm_service.usage_store import USAGE_TABLE_STATEMENTS
from shared.postgres import TeamSchema
from shared.postgres.secrets import SECRETS_TABLE_DDL

SCHEMA = TeamSchema(
    team="unified_api",
    database=None,
    statements=[SECRETS_TABLE_DDL, *PROVIDER_TABLE_STATEMENTS, *USAGE_TABLE_STATEMENTS],
    table_names=[
        "encrypted_integration_credentials",
        PROVIDER_TABLE_NAME,
        USAGE_TABLE_NAME,
    ],
)
