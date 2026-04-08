"""Postgres schema for the strands job service.

Owns the single ``jobs`` table that every agent team reads/writes via
``JobServiceClient``. The table lives in the ``strands_jobs`` database
(see `docker-compose.yml` — the container sets ``POSTGRES_DB`` to
``strands_jobs``, so ``database=None`` below resolves to it).
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="job_service",
    # None = use the container's POSTGRES_DB env var. In Docker this is
    # already ``strands_jobs``; in other contexts the caller may set
    # POSTGRES_DB explicitly.
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS jobs (
            job_id            TEXT NOT NULL,
            team              TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            data              JSONB NOT NULL DEFAULT '{}',
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (team, job_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_jobs_team_status ON jobs (team, status)",
        """CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat
            ON jobs (team, last_heartbeat_at)
            WHERE status IN ('pending', 'running')""",
    ],
)
