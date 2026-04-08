"""Postgres schema for the startup advisor team.

Ports ``backend/agents/startup_advisor/store.py`` (currently SQLite)
to Postgres. Registered from the team's FastAPI lifespan.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="startup_advisor",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS startup_advisor_conversations (
            conversation_id TEXT PRIMARY KEY,
            context_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS startup_advisor_conv_messages (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_startup_advisor_conv_messages_conv
            ON startup_advisor_conv_messages(conversation_id)""",
        """CREATE TABLE IF NOT EXISTS startup_advisor_conv_artifacts (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            artifact_type   TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            payload_json    JSONB NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_startup_advisor_conv_artifacts_conv
            ON startup_advisor_conv_artifacts(conversation_id)""",
    ],
)
