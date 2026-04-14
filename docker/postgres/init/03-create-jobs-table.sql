-- Job service schema: runs against khala_jobs database.
-- This file is executed by the Postgres entrypoint on first init as the
-- POSTGRES_USER superuser. We CREATE as superuser then explicitly ALTER
-- ownership to the `khala` role the job-service connects as — otherwise
-- khala gets "permission denied for table jobs" on every UPDATE/INSERT.
-- ALTER TABLE ... OWNER TO is used instead of SET ROLE so ownership is
-- unconditional and doesn't rely on session-level role inheritance.

\connect khala_jobs;

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

-- Defensive: transfer ownership of the table and its dependent objects to
-- the khala role. Indexes inherit the table's owner automatically.
ALTER TABLE jobs OWNER TO khala;
