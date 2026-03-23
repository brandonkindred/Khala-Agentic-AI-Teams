-- Job service schema: runs against strands_jobs database.
-- This file is executed by the Postgres entrypoint on first init.

\connect strands_jobs;

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
