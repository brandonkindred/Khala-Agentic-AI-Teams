"""Postgres schema owned by the user_profile module.

Pure data, no side effects at import time (Pattern B). The schema is
registered from the unified_api FastAPI lifespan via
``register_team_schemas(SCHEMA)``.

Two tables:

- ``user_profiles`` — one row per user. Single-tenant today: the only row
  is ``user_id = 'default'``, auto-created on first read. ``profile_json``
  holds free-form preferences so the profile can grow without migrations.
- ``user_profile_associations`` — the central registry linking a profile
  to artifacts produced by other teams (brands, blog posts, projects,
  agentic teams, integration configs). Teams record a link on create via
  ``user_profile.record_association``; nothing is copied, only referenced
  by ``(team, artifact_type, artifact_id)``.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="user_profile",
    database=None,  # default POSTGRES_DB
    statements=[
        """CREATE TABLE IF NOT EXISTS user_profiles (
            user_id      TEXT PRIMARY KEY,
            display_name TEXT NOT NULL DEFAULT '',
            email        TEXT NOT NULL DEFAULT '',
            bio          TEXT NOT NULL DEFAULT '',
            profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS user_profile_associations (
            id            TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            team          TEXT NOT NULL,
            artifact_id   TEXT NOT NULL,
            label         TEXT NOT NULL DEFAULT '',
            role          TEXT NOT NULL DEFAULT 'owner',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_user_profile_assoc_user "
        "ON user_profile_associations(user_id, artifact_type)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile_assoc_unique "
        "ON user_profile_associations(user_id, artifact_type, artifact_id)",
    ],
    table_names=[
        "user_profiles",
        "user_profile_associations",
    ],
)
