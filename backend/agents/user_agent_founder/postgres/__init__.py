"""Postgres schema for the user agent founder team.

Ports ``backend/agents/user_agent_founder/store.py`` (currently SQLite)
to Postgres. Registered from the team's FastAPI lifespan.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="user_agent_founder",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS user_agent_founder_runs (
            run_id          TEXT PRIMARY KEY,
            status          TEXT NOT NULL DEFAULT 'pending',
            se_job_id       TEXT,
            analysis_job_id TEXT,
            spec_content    TEXT,
            repo_path       TEXT,
            created_at      TIMESTAMPTZ NOT NULL,
            updated_at      TIMESTAMPTZ NOT NULL,
            error           TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS user_agent_founder_decisions (
            id             BIGSERIAL PRIMARY KEY,
            run_id         TEXT NOT NULL,
            question_id    TEXT NOT NULL,
            question_text  TEXT NOT NULL,
            answer_text    TEXT NOT NULL,
            rationale      TEXT NOT NULL DEFAULT '',
            timestamp      TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_user_agent_founder_decisions_run
            ON user_agent_founder_decisions(run_id)""",
    ],
)
