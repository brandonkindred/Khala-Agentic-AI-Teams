"""Postgres schema for the social media marketing team.

Declares the ``social_marketing_winning_posts`` table used by
``shared/winning_posts_bank.py``. Registered from the team's FastAPI
lifespan via ``shared_postgres.register_team_schemas``.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="social_marketing",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS social_marketing_winning_posts (
            id                TEXT PRIMARY KEY,
            title             TEXT NOT NULL,
            body              TEXT NOT NULL,
            platform          TEXT NOT NULL DEFAULT '',
            keywords          JSONB NOT NULL DEFAULT '[]'::jsonb,
            metrics           JSONB NOT NULL DEFAULT '{}'::jsonb,
            engagement_score  DOUBLE PRECISION NOT NULL DEFAULT 0,
            linked_goals      JSONB NOT NULL DEFAULT '[]'::jsonb,
            summary           TEXT NOT NULL DEFAULT '',
            source_job_id     TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_social_winning_posts_platform
            ON social_marketing_winning_posts(platform)""",
        """CREATE INDEX IF NOT EXISTS idx_social_winning_posts_source_job
            ON social_marketing_winning_posts(source_job_id)""",
        """CREATE INDEX IF NOT EXISTS idx_social_winning_posts_created
            ON social_marketing_winning_posts(created_at DESC)""",
        """CREATE INDEX IF NOT EXISTS idx_social_winning_posts_score
            ON social_marketing_winning_posts(engagement_score DESC)""",
    ],
    table_names=["social_marketing_winning_posts"],
)
