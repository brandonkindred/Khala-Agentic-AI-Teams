"""Postgres schema for the Software Engineering team's observability & learning layer.

Pattern B (see ``shared/postgres/README.md``): this module is *pure data* with no
side effects. The SE FastAPI app's lifespan registers it via
``register_team_schemas(SCHEMA)`` (a no-op when ``POSTGRES_HOST`` is unset).

Four tables:

- ``se_agent_traces`` — one row per LLM call (token counts, ``cost_usd``, latency,
  outcome). The optional Postgres trace sink behind ``SE_TRACE_TO_POSTGRES``; the
  DORA/cost endpoint reads cost from here so metrics work without an OTLP collector.
- ``se_events`` — pipeline lifecycle events (task created/merged, gate
  rejections/re-entries, crash detected/resolved). The DORA-metrics substrate.
- ``se_learnings`` — distilled lessons (``pattern`` / ``trigger`` /
  ``counter_measure``) with a generated full-text-search column, ingested from
  post-mortems and quality-gate rejections and injected back into the Tech Lead's
  Design prompt.
- ``code_review_runs`` — one row per executed PR code review (coding_team
  sub-team), so the Code Review page can show every review run for a pull
  request — with its status and outcome — and have that history survive page
  reloads and restarts.
- ``code_review_transcripts`` — one row per PR code review job, holding every
  LLM call the review pipeline made (stage, target, prompt, response) as an
  ordered JSONB array, so a completed review's full transcript can be
  inspected after the fact. Rows are appended to incrementally as the review
  runs (see ``review_history_store.append_review_transcript_entry``), keyed
  1:1 with ``code_review_runs.job_id``.
"""

from __future__ import annotations

from shared.postgres import TeamSchema

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
            detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
            trace_id   TEXT NOT NULL DEFAULT ''
        )""",
        # Idempotent migration for tables created before trace_id existed: the
        # CREATE TABLE above is a no-op against an existing table, so without
        # this, inserts/selects referencing trace_id would fail on deployments
        # upgrading an existing database.
        "ALTER TABLE se_events ADD COLUMN IF NOT EXISTS trace_id TEXT NOT NULL DEFAULT ''",
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
        # One row per PR review run, keyed by the review job id. ``review_summary``
        # holds the same shape written onto the job (total_issues, inline_comments,
        # comment_findings, event, files_reviewed).
        """CREATE TABLE IF NOT EXISTS code_review_runs (
            job_id          TEXT PRIMARY KEY,
            owner           TEXT NOT NULL,
            repo            TEXT NOT NULL,
            pr_number       INTEGER NOT NULL,
            pr_url          TEXT,
            status          TEXT NOT NULL,
            status_text     TEXT,
            review_summary  JSONB,
            error           TEXT,
            author          TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at    TIMESTAMPTZ
        )""",
        # The page lists reviews per (repo, PR), newest first.
        """CREATE INDEX IF NOT EXISTS idx_code_review_runs_pr
            ON code_review_runs(owner, repo, pr_number, created_at DESC)""",
        # ``list_reviews`` matches owner/repo case-insensitively (GitHub treats them so, and
        # rows may carry operator-typed casing while lookups use the canonical GET /user/repos
        # casing). A functional index on the lowered columns keeps that query sargable — the
        # plain (owner, repo, …) index above can't serve a ``lower(owner) = lower(%s)`` predicate.
        """CREATE INDEX IF NOT EXISTS idx_code_review_runs_pr_ci
            ON code_review_runs(lower(owner), lower(repo), pr_number, created_at DESC)""",
        # One row per review job; ``entries`` is appended to (never replaced) as
        # the review runs — see ``append_review_transcript_entry``.
        """CREATE TABLE IF NOT EXISTS code_review_transcripts (
            job_id       TEXT PRIMARY KEY REFERENCES code_review_runs(job_id) ON DELETE CASCADE,
            entries      JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ],
    table_names=[
        "se_agent_traces",
        "se_events",
        "se_learnings",
        "code_review_runs",
        "code_review_transcripts",
    ],
)

__all__ = ["SCHEMA"]
