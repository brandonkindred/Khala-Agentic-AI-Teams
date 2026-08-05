"""Postgres schema for the Agent Studio authoring-conversation store.

Pure data module — importing it has no side effects. DDL runs when the unified
API lifespan calls ``shared.postgres.register_team_schemas(SCHEMA)``.

Backs the durable, cross-worker authoring conversation store (the in-memory store
is retained for the Postgres-less local/dev path). A conversation's in-progress
:class:`~agent_team_studio.agent_studio.models.AgentDefinition` lives in ``definition_json``; each
turn's messages are rows in ``agent_studio_conv_messages``. Per-conversation turn
serialization uses a ``SELECT … FOR UPDATE`` row lock on the conversation row.
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
    ],
    table_names=[
        # Messages first: FK-dependent child truncated before its parent.
        "agent_studio_conv_messages",
        "agent_studio_conversations",
    ],
)
