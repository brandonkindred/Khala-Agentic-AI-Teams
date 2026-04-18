"""Postgres schema for the Agent Console team.

Pure data module — importing it has no side effects. DDL runs when the
unified API lifespan calls
``shared_postgres.register_team_schemas(SCHEMA)``.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="agent_console",
    database=None,
    statements=[
        # -----------------------------------------------------------------
        # Saved inputs — user-curated payloads per agent.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_console_saved_inputs (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            name            TEXT NOT NULL,
            input_data      JSONB NOT NULL,
            author          TEXT NOT NULL,
            description     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_console_saved_inputs_agent
            ON agent_console_saved_inputs(agent_id)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_console_saved_inputs_agent_name
            ON agent_console_saved_inputs(agent_id, name)""",
        # -----------------------------------------------------------------
        # Runs — one row per successful or failed invocation.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_console_runs (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            team            TEXT NOT NULL,
            saved_input_id  TEXT,
            input_data      JSONB NOT NULL,
            output_data     JSONB,
            error           TEXT,
            status          TEXT NOT NULL,
            duration_ms     INTEGER NOT NULL DEFAULT 0,
            trace_id        TEXT NOT NULL,
            logs_tail       JSONB NOT NULL DEFAULT '[]'::jsonb,
            author          TEXT NOT NULL,
            sandbox_url     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_console_runs_agent_created
            ON agent_console_runs(agent_id, created_at DESC)""",
        """CREATE INDEX IF NOT EXISTS idx_agent_console_runs_saved_input
            ON agent_console_runs(saved_input_id)""",
    ],
    table_names=[
        "agent_console_saved_inputs",
        "agent_console_runs",
    ],
)
