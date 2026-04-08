"""Postgres schema for the generic team assistant conversation store.

Ports ``backend/agents/team_assistant/store.py`` (currently SQLite) to
Postgres. The assistant sub-apps are hosted in-process by the unified
API, so this schema is registered from the unified_api lifespan when
the team_assistant module is imported for mounting.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="team_assistant",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS team_assistant_conversations (
            conversation_id TEXT PRIMARY KEY,
            job_id          TEXT,
            context_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at      TIMESTAMPTZ NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_team_assistant_conversations_job_id
            ON team_assistant_conversations(job_id)""",
        """CREATE TABLE IF NOT EXISTS team_assistant_conv_messages (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_team_assistant_conv_messages_conv
            ON team_assistant_conv_messages(conversation_id)""",
        """CREATE TABLE IF NOT EXISTS team_assistant_conv_artifacts (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            artifact_type   TEXT NOT NULL,
            title           TEXT NOT NULL DEFAULT '',
            payload_json    JSONB NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_team_assistant_conv_artifacts_conv
            ON team_assistant_conv_artifacts(conversation_id)""",
    ],
)
