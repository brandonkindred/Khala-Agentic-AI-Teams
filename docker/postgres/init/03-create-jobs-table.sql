-- Job service schema: runs against khala_jobs database.
-- This file is executed by the Postgres entrypoint on first init as the
-- POSTGRES_USER superuser. We SET ROLE to khala first so the table (and any
-- later schema changes) are owned by the same role the job-service connects
-- as — otherwise the khala role gets "permission denied for table jobs"
-- when it tries to UPDATE/INSERT.

\connect khala_jobs;

SET ROLE khala;

CREATE TABLE IF NOT EXISTS jobs (
    job_id            TEXT NOT NULL,
    team              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    data              JSONB NOT NULL DEFAULT '{}',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team, job_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_team_status ON jobs (team, status);
CREATE INDEX IF NOT EXISTS idx_jobs_heartbeat ON jobs (team, last_heartbeat_at)
    WHERE status IN ('pending', 'running');

RESET ROLE;
