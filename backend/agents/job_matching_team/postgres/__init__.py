"""Postgres schema for the job matching team.

Two tables: one row per scan run, and one row per ranked posting within a run.
Registered from the team's FastAPI lifespan via
``shared_postgres.register_team_schemas``.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="job_matching",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS job_matching_runs (
            run_id           TEXT PRIMARY KEY,
            status           TEXT NOT NULL,
            profile_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
            request_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
            top_n            INTEGER NOT NULL DEFAULT 0,
            total_found      INTEGER NOT NULL DEFAULT 0,
            total_ranked     INTEGER NOT NULL DEFAULT 0,
            seen_fingerprints JSONB NOT NULL DEFAULT '[]'::jsonb,
            error            TEXT,
            created_at       TIMESTAMPTZ NOT NULL,
            completed_at     TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS job_matching_ranked_jobs (
            id             BIGSERIAL PRIMARY KEY,
            run_id         TEXT NOT NULL,
            rank           INTEGER NOT NULL,
            score          DOUBLE PRECISION NOT NULL DEFAULT 0,
            sub_scores     JSONB NOT NULL DEFAULT '{}'::jsonb,
            posting        JSONB NOT NULL DEFAULT '{}'::jsonb,
            recommendation TEXT NOT NULL DEFAULT 'maybe',
            rationale      TEXT NOT NULL DEFAULT '',
            concerns       JSONB NOT NULL DEFAULT '[]'::jsonb,
            fingerprint    TEXT NOT NULL DEFAULT '',
            created_at     TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_job_matching_ranked_jobs_run
            ON job_matching_ranked_jobs(run_id)""",
        """CREATE INDEX IF NOT EXISTS idx_job_matching_ranked_jobs_fingerprint
            ON job_matching_ranked_jobs(fingerprint)""",
    ],
    table_names=[
        "job_matching_runs",
        "job_matching_ranked_jobs",
    ],
)
