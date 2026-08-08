"""Postgres schema for Agent Studio conversation + drafts stores.

Pure data module — importing it has no side effects. DDL runs when the unified
API lifespan calls ``shared.postgres.register_team_schemas(SCHEMA)``.

Backs (1) the durable authoring conversation store and (2) the user-scoped
``agent_studio_drafts`` table for save/resume of Studio handoff + stage work.
"""

from __future__ import annotations

from shared.postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="agent_studio",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS agent_studio_conversations (
            conversation_id  TEXT PRIMARY KEY,
            mode             TEXT NOT NULL,
            source_agent_id  TEXT,
            definition_json  JSONB NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS agent_studio_conv_messages (
            id               BIGSERIAL PRIMARY KEY,
            conversation_id  TEXT NOT NULL
                REFERENCES agent_studio_conversations(conversation_id) ON DELETE CASCADE,
            role             TEXT NOT NULL,
            content          TEXT NOT NULL,
            timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_studio_conv_messages_conv
            ON agent_studio_conv_messages(conversation_id, id)""",
        """CREATE TABLE IF NOT EXISTS agent_studio_drafts (
            draft_id     TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL,
            name         TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_studio_drafts_user_updated
            ON agent_studio_drafts (user_id, updated_at DESC)""",
    ],
    table_names=[
        # Messages first: FK-dependent child truncated before its parent.
        "agent_studio_conv_messages",
        "agent_studio_conversations",
        "agent_studio_drafts",
    ],
)
