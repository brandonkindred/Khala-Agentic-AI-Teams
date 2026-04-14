"""Postgres schema owned by the unified_api.

Declares the single ``encrypted_integration_credentials`` table that
``postgres_encrypted_credentials.py`` and ``google_browser_login_credentials.py``
both read/write. Registered from the unified_api's FastAPI lifespan.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="unified_api",
    database=None,  # default POSTGRES_DB
    statements=[
        """CREATE TABLE IF NOT EXISTS encrypted_integration_credentials (
            service TEXT NOT NULL,
            credential_key TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (service, credential_key)
        )""",
    ],
    table_names=[
        "encrypted_integration_credentials",
    ],
)
