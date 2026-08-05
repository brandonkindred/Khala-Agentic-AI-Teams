"""Postgres schema for the user agent founder team.

Ports ``backend/agents/agent_team_studio/user_agent_founder/store.py`` (currently SQLite)
to Postgres. Registered from the team's FastAPI lifespan.
"""

from __future__ import annotations

from shared.postgres import TeamSchema

SCHEMA = TeamSchema(
    team="user_agent_founder",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS user_agent_founder_runs (
            run_id           TEXT PRIMARY KEY,
            status           TEXT NOT NULL DEFAULT 'pending',
            se_job_id        TEXT,
            analysis_job_id  TEXT,
            spec_content     TEXT,
            repo_path        TEXT,
            target_team_key  TEXT NOT NULL DEFAULT 'software_engineering',
            created_at       TIMESTAMPTZ NOT NULL,
            updated_at       TIMESTAMPTZ NOT NULL,
            error            TEXT
        )""",
        # Idempotent migration for existing deployments where the table
        # was created before this column existed.
        """ALTER TABLE user_agent_founder_runs
            ADD COLUMN IF NOT EXISTS target_team_key TEXT
            NOT NULL DEFAULT 'software_engineering'""",
        """ALTER TABLE user_agent_founder_runs
            ADD COLUMN IF NOT EXISTS persona_id TEXT""",
        """ALTER TABLE user_agent_founder_runs
            ADD COLUMN IF NOT EXISTS project_name TEXT""",
        # The agentic-team persona target records the chosen process id here so
        # the AgenticTeamAdapter can drive that specific process (rather than
        # overloading repo_path — see targets/agentic_team.py). Nullable: the
        # software-engineering target leaves it unset.
        """ALTER TABLE user_agent_founder_runs
            ADD COLUMN IF NOT EXISTS process_id TEXT""",
        # Backfill the default persona only for *agentic-team* runs that predate
        # the persona_id column. Scoped to ``agentic_team:%`` so pre-existing
        # software-engineering runs keep a NULL persona_id (they are not persona
        # runs and must not be mislabeled as such). project_name is intentionally
        # left NULL rather than stamped with a placeholder — a synthetic name would
        # misrepresent the run.
        """UPDATE user_agent_founder_runs
            SET persona_id = 'startup-founder'
            WHERE target_team_key LIKE 'agentic_team:%' AND persona_id IS NULL""",
        """CREATE TABLE IF NOT EXISTS user_agent_founder_personas (
            persona_id              TEXT PRIMARY KEY,
            name                    TEXT NOT NULL,
            description             TEXT NOT NULL,
            icon                    TEXT NOT NULL DEFAULT 'person',
            system_prompt           TEXT NOT NULL,
            spec_generation_prompt  TEXT NOT NULL,
            is_builtin              BOOLEAN NOT NULL DEFAULT FALSE,
            created_at              TIMESTAMPTZ NOT NULL,
            updated_at              TIMESTAMPTZ NOT NULL
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
        """CREATE TABLE IF NOT EXISTS user_agent_founder_chat_messages (
            id             BIGSERIAL PRIMARY KEY,
            run_id         TEXT NOT NULL,
            role           TEXT NOT NULL,
            content        TEXT NOT NULL,
            message_type   TEXT NOT NULL DEFAULT 'chat',
            metadata       JSONB,
            timestamp      TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_uaf_chat_messages_run
            ON user_agent_founder_chat_messages(run_id)""",
    ],
    table_names=[
        "user_agent_founder_runs",
        "user_agent_founder_decisions",
        "user_agent_founder_chat_messages",
        "user_agent_founder_personas",
    ],
)
