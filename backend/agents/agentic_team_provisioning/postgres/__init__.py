"""Postgres schema for the agentic team provisioning team.

Ports ``backend/agents/agentic_team_provisioning/assistant/store.py`` and
``backend/agents/agentic_team_provisioning/infrastructure.py`` (currently
SQLite) to Postgres. Registered from the team's FastAPI lifespan.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="agentic_team_provisioning",
    database=None,
    statements=[
        # assistant/store.py
        """CREATE TABLE IF NOT EXISTS agentic_teams (
            team_id     TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            created_at  TIMESTAMPTZ NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS agentic_processes (
            process_id TEXT PRIMARY KEY,
            team_id    TEXT NOT NULL REFERENCES agentic_teams(team_id),
            data_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_processes_team ON agentic_processes(team_id)",
        """CREATE TABLE IF NOT EXISTS agentic_conversations (
            conversation_id TEXT PRIMARY KEY,
            team_id         TEXT NOT NULL REFERENCES agentic_teams(team_id),
            process_id      TEXT,
            created_at      TIMESTAMPTZ NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_conversations_team ON agentic_conversations(team_id)",
        """CREATE TABLE IF NOT EXISTS agentic_conv_messages (
            id              BIGSERIAL PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES agentic_conversations(conversation_id),
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            timestamp       TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agentic_conv_messages_conv
            ON agentic_conv_messages(conversation_id)""",
        """CREATE TABLE IF NOT EXISTS agentic_team_agents (
            team_id    TEXT NOT NULL REFERENCES agentic_teams(team_id),
            agent_name TEXT NOT NULL,
            data_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (team_id, agent_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_team_agents_team ON agentic_team_agents(team_id)",
        """CREATE TABLE IF NOT EXISTS agentic_env_provisions (
            team_id               TEXT NOT NULL,
            stable_key            TEXT NOT NULL,
            process_id            TEXT NOT NULL,
            step_id               TEXT NOT NULL,
            agent_name            TEXT NOT NULL,
            provisioning_agent_id TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'running',
            error_message         TEXT,
            created_at            TIMESTAMPTZ NOT NULL,
            updated_at            TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (team_id, stable_key)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_env_provisions_team ON agentic_env_provisions(team_id)",
        # infrastructure.py — form_data (namespaced so a future migration
        # can move per-team SQLite form stores into the shared DB).
        """CREATE TABLE IF NOT EXISTS agentic_form_data (
            record_id  TEXT PRIMARY KEY,
            team_id    TEXT NOT NULL,
            form_key   TEXT NOT NULL,
            data_json  JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_form_data_team ON agentic_form_data(team_id)",
        "CREATE INDEX IF NOT EXISTS idx_agentic_form_data_key ON agentic_form_data(form_key)",
    ],
)
