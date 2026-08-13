"""Postgres schema for the Agent Registry's dynamic-manifest overlay.

Pure data module — importing it has no side effects (no psycopg import, no
connection). DDL runs when the unified API lifespan calls
``shared.postgres.register_team_schemas(SCHEMA)``.

This table backs cross-worker visibility of **dynamically registered** manifests
(Agent Studio saves + ``agentic_team_provisioning`` generated agents). Disk-YAML
catalog manifests are never written here — they are already present on every
worker's filesystem (and in every sandbox image). See ``dynamic_store``.
"""

from __future__ import annotations

from shared.postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="agent_registry",
    database=None,
    statements=[
        # One row per dynamically registered manifest. ``id`` mirrors
        # ``AgentManifest.id``; ``manifest`` is the full JSON-mode dump so a read
        # round-trips to an identical validated manifest. ``team`` / ``tags`` are
        # denormalized copies to keep catalog filtering cheap, but the ``manifest``
        # column is authoritative.
        """CREATE TABLE IF NOT EXISTS agent_registry_dynamic_manifests (
            id          TEXT PRIMARY KEY,
            team        TEXT NOT NULL,
            tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
            manifest    JSONB NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_registry_dynamic_team
            ON agent_registry_dynamic_manifests(team)""",
    ],
    table_names=[
        "agent_registry_dynamic_manifests",
    ],
)
