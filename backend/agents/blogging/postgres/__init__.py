"""Postgres schema for the blogging team.

Ports ``backend/agents/blogging/shared/story_bank.py`` (currently
SQLite) to Postgres. Registered from the blogging service's FastAPI
lifespan.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="blogging",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS blogging_stories (
            id              TEXT PRIMARY KEY,
            narrative       TEXT NOT NULL,
            section_title   TEXT NOT NULL DEFAULT '',
            section_context TEXT NOT NULL DEFAULT '',
            keywords        JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary         TEXT NOT NULL DEFAULT '',
            source_job_id   TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_blogging_stories_source_job
            ON blogging_stories(source_job_id)""",
    ],
)
