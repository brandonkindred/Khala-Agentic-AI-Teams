"""Postgres schema for the branding team.

Pure-data declaration — no side effects on import. The team's FastAPI
lifespan calls ``register_team_schemas(SCHEMA)`` at startup. Tables are
prefixed with ``branding_`` to avoid collisions in the shared
``POSTGRES_DB``.

Current persistence is SQLite (see ``branding_team/store.py`` and
``branding_team/assistant/store.py``); these DDL statements are the
Postgres port ready for a future data migration.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="branding",
    database=None,  # default POSTGRES_DB
    statements=[
        # store.py — clients + brand versions
        """CREATE TABLE IF NOT EXISTS branding_clients (
            id         TEXT PRIMARY KEY,
            data       JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS branding_brands (
            id         TEXT PRIMARY KEY,
            client_id  TEXT NOT NULL,
            data       JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_branding_brands_client ON branding_brands(client_id)",
        # api/main.py — per-session store
        """CREATE TABLE IF NOT EXISTS branding_sessions (
            session_id   TEXT PRIMARY KEY,
            session_json JSONB NOT NULL,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        # assistant/store.py — conversation store
        """CREATE TABLE IF NOT EXISTS branding_conversations (
            conversation_id    TEXT PRIMARY KEY,
            brand_id           TEXT,
            mission_json       JSONB NOT NULL,
            latest_output_json JSONB,
            created_at         TIMESTAMPTZ NOT NULL,
            updated_at         TIMESTAMPTZ NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS branding_conv_messages (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_branding_conv_messages_conv
            ON branding_conv_messages(conversation_id)""",
    ],
)
