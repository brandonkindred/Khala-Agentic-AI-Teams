"""Postgres schema for the coding team — persisted PR code-review history.

Pure data (Pattern B): this module has no import side effects. The coding-team
FastAPI lifespan calls ``register_team_schemas(SCHEMA)`` at startup, and the
unified API may register the same schema (registration is idempotent).

The ``code_review_runs`` table records one row per executed PR code review so
the Code Review page can show every review run for a pull request — with its
status and outcome — and have that history survive page reloads and restarts.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="coding_team",
    database=None,
    statements=[
        # One row per review run, keyed by the review job id. ``review_summary``
        # holds the same shape written onto the job (total_issues, inline_comments,
        # body_findings, event, files_reviewed).
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
    ],
    table_names=["code_review_runs"],
)

__all__ = ["SCHEMA"]
