"""Postgres schema for the Software Engineering team's observability & learning layer.

Pattern B (see ``shared_postgres/README.md``): this module is *pure data* with no
side effects. The SE FastAPI app's lifespan registers it via
``register_team_schemas(SCHEMA)`` (a no-op when ``POSTGRES_HOST`` is unset).

Three tables:

- ``se_agent_traces`` — one row per LLM call (token counts, ``cost_usd``, latency,
  outcome). The optional Postgres trace sink behind ``SE_TRACE_TO_POSTGRES``; the
  DORA/cost endpoint reads cost from here so metrics work without an OTLP collector.
- ``se_events`` — pipeline lifecycle events (task created/merged, gate
  rejections/re-entries, crash detected/resolved). The DORA-metrics substrate.
- ``se_learnings`` — distilled lessons (``pattern`` / ``trigger`` /
  ``counter_measure``) with a generated full-text-search column, ingested from
  post-mortems and quality-gate rejections and injected back into the Tech Lead's
  Design prompt.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="software_engineering",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS se_agent_traces (
            id            BIGSERIAL PRIMARY KEY,
            ts            TIMESTAMPTZ NOT NULL,
            team          TEXT NOT NULL DEFAULT '',
            agent_key     TEXT NOT NULL DEFAULT '',
            job_id        TEXT NOT NULL DEFAULT '',
            task_id       TEXT NOT NULL DEFAULT '',
            phase         TEXT NOT NULL DEFAULT '',
            model         TEXT NOT NULL DEFAULT '',
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens  INTEGER NOT NULL DEFAULT 0,
            cost_usd      DOUBLE PRECISION NOT NULL DEFAULT 0,
            latency_ms    INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT '',
            outcome       TEXT NOT NULL DEFAULT '',
            objective     TEXT NOT NULL DEFAULT '',
            request_id    TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_se_agent_traces_job ON se_agent_traces(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_se_agent_traces_ts ON se_agent_traces(ts)",
        "CREATE INDEX IF NOT EXISTS idx_se_agent_traces_phase ON se_agent_traces(phase)",
        """CREATE TABLE IF NOT EXISTS se_events (
            id         BIGSERIAL PRIMARY KEY,
            ts         TIMESTAMPTZ NOT NULL,
            job_id     TEXT NOT NULL DEFAULT '',
            task_id    TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            phase      TEXT NOT NULL DEFAULT '',
            gate       TEXT NOT NULL DEFAULT '',
            detail     JSONB NOT NULL DEFAULT '{}'::jsonb
        )""",
        "CREATE INDEX IF NOT EXISTS idx_se_events_type_ts ON se_events(event_type, ts)",
        "CREATE INDEX IF NOT EXISTS idx_se_events_job ON se_events(job_id)",
        """CREATE TABLE IF NOT EXISTS se_learnings (
            id              BIGSERIAL PRIMARY KEY,
            fingerprint     TEXT NOT NULL UNIQUE,
            pattern         TEXT NOT NULL,
            trigger         TEXT NOT NULL DEFAULT '',
            counter_measure TEXT NOT NULL DEFAULT '',
            source          TEXT NOT NULL DEFAULT '',
            category        TEXT NOT NULL DEFAULT '',
            occurrences     INTEGER NOT NULL DEFAULT 1,
            created_at      TIMESTAMPTZ NOT NULL,
            last_seen       TIMESTAMPTZ NOT NULL,
            search_tsv      tsvector GENERATED ALWAYS AS (
                to_tsvector('english', coalesce(pattern, '') || ' ' || coalesce(trigger, ''))
            ) STORED
        )""",
        "CREATE INDEX IF NOT EXISTS idx_se_learnings_tsv ON se_learnings USING GIN(search_tsv)",
        "CREATE INDEX IF NOT EXISTS idx_se_learnings_category ON se_learnings(category)",
    ],
    table_names=["se_agent_traces", "se_events", "se_learnings"],
)

__all__ = ["SCHEMA"]
