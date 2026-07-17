"""Postgres schema for the Planning team.

Pure data module — importing it has no side effects. DDL runs when the
team's FastAPI lifespan calls ``shared_postgres.register_team_schemas(SCHEMA)``.

One table, ``planning_runs``: an audit record of each planning run, keyed by
``job_id``, holding the client name, run summary, handoff summary, and the
open/resolved discovery questions as JSONB. See ``postgres.writer`` for the
best-effort writer that populates it at run finalize.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="planning",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS planning_runs (
            job_id             TEXT PRIMARY KEY,
            client_name        TEXT,
            summary            TEXT NOT NULL DEFAULT '',
            handoff_summary    TEXT NOT NULL DEFAULT '',
            open_questions     JSONB NOT NULL DEFAULT '[]'::jsonb,
            resolved_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_planning_runs_created ON planning_runs(created_at)""",
    ],
    table_names=["planning_runs"],
)
