"""Postgres schema for the nutrition & meal planning team.

Ports the four file-based stores under
``nutrition_meal_planning_team/shared/`` (client profiles, conversation
history, nutrition plans, meal recommendations + feedback) to Postgres.
Registered from the team's FastAPI lifespan via
``shared_postgres.register_team_schemas``.

This module is pure data — importing it has no side effects. DDL runs
only when ``register_team_schemas(SCHEMA)`` is invoked at startup.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA = TeamSchema(
    team="nutrition_meal_planning",
    database=None,
    statements=[
        """CREATE TABLE IF NOT EXISTS nutrition_profiles (
            client_id   TEXT PRIMARY KEY,
            profile     JSONB NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS nutrition_conversations (
            id         BIGSERIAL PRIMARY KEY,
            client_id  TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            phase      TEXT,
            action     TEXT,
            timestamp  TIMESTAMPTZ NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_conversations_client
            ON nutrition_conversations(client_id, id)""",
        """CREATE TABLE IF NOT EXISTS nutrition_plans (
            client_id     TEXT PRIMARY KEY,
            profile_hash  TEXT NOT NULL,
            plan          JSONB NOT NULL,
            generated_at  TIMESTAMPTZ NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS nutrition_recommendations (
            recommendation_id         TEXT PRIMARY KEY,
            client_id                 TEXT NOT NULL,
            meal_snapshot             JSONB NOT NULL,
            recommended_at            TIMESTAMPTZ NOT NULL,
            feedback_rating           INTEGER,
            feedback_would_make_again BOOLEAN,
            feedback_notes            TEXT,
            feedback_submitted_at     TIMESTAMPTZ
        )""",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_recommendations_client_time
            ON nutrition_recommendations(client_id, recommended_at DESC)""",
        # --- SPEC-002 additions (biometric + clinical audit trails) ---
        # Append-only log of every biometric write. ``nutrition_profiles``
        # already carries the latest value inside its JSONB; this table
        # is the time-series view SPEC-002 / ADR-006 read for charts
        # and weight-trend events. Future device integrations
        # (SPEC-019) ride on top of the same shape.
        """CREATE TABLE IF NOT EXISTS nutrition_biometric_log (
            id              BIGSERIAL PRIMARY KEY,
            client_id       TEXT NOT NULL,
            field           TEXT NOT NULL,
            value_numeric   DOUBLE PRECISION,
            value_text      TEXT,
            unit            TEXT,
            source          TEXT NOT NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            recorded_by     TEXT
        )""",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_biometric_log_client_field_time
            ON nutrition_biometric_log(client_id, field, recorded_at DESC)""",
        # Audit log for clinician-authored numeric overrides on the
        # profile (e.g. ``{"bmi_floor": 19.5}``). Every write produces
        # a row; the live value lives on ClientProfile.clinical.
        """CREATE TABLE IF NOT EXISTS nutrition_clinical_overrides_log (
            id              BIGSERIAL PRIMARY KEY,
            client_id       TEXT NOT NULL,
            key             TEXT NOT NULL,
            value_numeric   DOUBLE PRECISION,
            reason          TEXT,
            author          TEXT NOT NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_clinical_overrides_log_client_time
            ON nutrition_clinical_overrides_log(client_id, recorded_at DESC)""",
        # --- SPEC-004 additions (calculator-version-aware plan cache) ---
        # Adds columns used by the new cache key. Existing rows keep
        # their NULL values and are correctly treated as cache misses
        # on the new read path, which forces one regeneration per
        # client on first read post-migration.
        "ALTER TABLE nutrition_plans ADD COLUMN IF NOT EXISTS calculator_version TEXT",
        "ALTER TABLE nutrition_plans ADD COLUMN IF NOT EXISTS profile_cache_vector TEXT",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_plans_cache_key
            ON nutrition_plans(client_id, calculator_version, profile_cache_vector)""",
        # --- SPEC-006 additions (restriction resolution) ---
        # ``restriction_resolution`` mirrors the RestrictionResolution
        # field inside ``profile`` JSONB; the separate column exists only
        # so future scanners can query without parsing the whole profile.
        # ``restriction_resolver_kb_version`` is a denormalized index key
        # for the SPEC-006 W9 background re-resolver (finds profiles with
        # an older KB version).
        "ALTER TABLE nutrition_profiles ADD COLUMN IF NOT EXISTS "
        "restriction_resolution JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE nutrition_profiles ADD COLUMN IF NOT EXISTS "
        "restriction_resolver_kb_version TEXT",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_profiles_resolver_kb_version
            ON nutrition_profiles(restriction_resolver_kb_version)""",
        # --- SPEC-015 additions (pantry tracking + bulk-import drafts) ---
        # One row per (client_id, canonical_id). Re-adding an existing
        # canonical id on insert increments ``quantity_grams`` rather than
        # producing a duplicate (enforced at the store layer via ON
        # CONFLICT). Cascade delete on profile removal.
        """CREATE TABLE IF NOT EXISTS nutrition_pantry (
            client_id       TEXT NOT NULL REFERENCES nutrition_profiles(client_id) ON DELETE CASCADE,
            canonical_id    TEXT NOT NULL,
            quantity_grams  DOUBLE PRECISION NOT NULL,
            display_qty     DOUBLE PRECISION,
            display_unit    TEXT,
            expires_on      DATE,
            notes           TEXT,
            added_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (client_id, canonical_id)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_nutrition_pantry_client_expires
            ON nutrition_pantry (client_id, expires_on) WHERE expires_on IS NOT NULL""",
        # Short-lived drafts produced by the SPEC-015 bulk-import parser
        # (two-step parse → confirm flow). Populated by W6; migrated now
        # so we only touch the schema once for this spec.
        """CREATE TABLE IF NOT EXISTS nutrition_pantry_import_drafts (
            draft_id     TEXT PRIMARY KEY,
            client_id    TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )""",
    ],
    table_names=[
        "nutrition_profiles",
        "nutrition_conversations",
        "nutrition_plans",
        "nutrition_recommendations",
        "nutrition_biometric_log",
        "nutrition_clinical_overrides_log",
        "nutrition_pantry",
        "nutrition_pantry_import_drafts",
    ],
)
