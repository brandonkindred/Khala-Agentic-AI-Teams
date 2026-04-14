"""Postgres schema for the agentic team provisioning team.

Registered from the team's FastAPI lifespan. Tables are prefixed with
``agentic_`` to avoid collisions in the shared ``POSTGRES_DB``.
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
        # infrastructure.py — form_data partitioned by team_id.
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
        # --- Interactive Testing Mode tables ---
        "ALTER TABLE agentic_teams ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'development'",
        """CREATE TABLE IF NOT EXISTS agentic_test_chat_sessions (
            session_id   TEXT PRIMARY KEY,
            team_id      TEXT NOT NULL REFERENCES agentic_teams(team_id),
            agent_name   TEXT NOT NULL,
            session_name TEXT NOT NULL DEFAULT '',
            created_at   TIMESTAMPTZ NOT NULL,
            updated_at   TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_test_chat_sessions_team ON agentic_test_chat_sessions(team_id)",
        """CREATE TABLE IF NOT EXISTS agentic_test_chat_messages (
            message_id  TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL REFERENCES agentic_test_chat_sessions(session_id) ON DELETE CASCADE,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            rating      TEXT,
            created_at  TIMESTAMPTZ NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_test_chat_messages_session ON agentic_test_chat_messages(session_id)",
        """CREATE TABLE IF NOT EXISTS agentic_test_pipeline_runs (
            run_id          TEXT PRIMARY KEY,
            team_id         TEXT NOT NULL REFERENCES agentic_teams(team_id),
            process_id      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'running',
            current_step_id TEXT,
            initial_input   TEXT,
            step_results    JSONB NOT NULL DEFAULT '[]'::jsonb,
            human_prompt    TEXT,
            error           TEXT,
            started_at      TIMESTAMPTZ NOT NULL,
            finished_at     TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS idx_agentic_test_pipeline_runs_team ON agentic_test_pipeline_runs(team_id, started_at DESC)",
    ],
    table_names=[
        "agentic_teams",
        "agentic_processes",
        "agentic_conversations",
        "agentic_conv_messages",
        "agentic_team_agents",
        "agentic_env_provisions",
        "agentic_form_data",
        "agentic_test_chat_sessions",
        "agentic_test_chat_messages",
        "agentic_test_pipeline_runs",
    ],
)
