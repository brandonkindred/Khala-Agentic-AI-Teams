"""Postgres schema for the Product Delivery team.

Pure data module — importing it has no side effects. DDL runs when the
unified API lifespan calls
``shared_postgres.register_team_schemas(SCHEMA)``.

Schema shape (Phase 1 of issue #243):

    products
      └── initiatives
            └── epics
                  └── stories
                        ├── tasks
                        └── acceptance_criteria
    feedback_items   (optionally linked to a story)

Sprints / releases ship in the follow-up — they belong on this same
schema and will be added as additional ``CREATE TABLE IF NOT EXISTS``
statements (no destructive migration required).
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="product_delivery",
    database=None,
    statements=[
        # -----------------------------------------------------------------
        # Products — top-level container.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS product_delivery_products (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            vision      TEXT NOT NULL DEFAULT '',
            author      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        # -----------------------------------------------------------------
        # Initiatives → Epics → Stories → (Tasks | AC).
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS product_delivery_initiatives (
            id          TEXT PRIMARY KEY,
            product_id  TEXT NOT NULL REFERENCES product_delivery_products(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'proposed',
            wsjf_score  DOUBLE PRECISION,
            rice_score  DOUBLE PRECISION,
            author      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_initiatives_product
            ON product_delivery_initiatives(product_id)""",
        """CREATE TABLE IF NOT EXISTS product_delivery_epics (
            id            TEXT PRIMARY KEY,
            initiative_id TEXT NOT NULL REFERENCES product_delivery_initiatives(id) ON DELETE CASCADE,
            title         TEXT NOT NULL,
            summary       TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'proposed',
            wsjf_score    DOUBLE PRECISION,
            rice_score    DOUBLE PRECISION,
            author        TEXT NOT NULL,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_epics_initiative
            ON product_delivery_epics(initiative_id)""",
        """CREATE TABLE IF NOT EXISTS product_delivery_stories (
            id              TEXT PRIMARY KEY,
            epic_id         TEXT NOT NULL REFERENCES product_delivery_epics(id) ON DELETE CASCADE,
            title           TEXT NOT NULL,
            user_story      TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'proposed',
            wsjf_score      DOUBLE PRECISION,
            rice_score      DOUBLE PRECISION,
            estimate_points DOUBLE PRECISION,
            author          TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_stories_epic
            ON product_delivery_stories(epic_id)""",
        """CREATE TABLE IF NOT EXISTS product_delivery_tasks (
            id          TEXT PRIMARY KEY,
            story_id    TEXT NOT NULL REFERENCES product_delivery_stories(id) ON DELETE CASCADE,
            title       TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'todo',
            owner       TEXT,
            author      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_tasks_story
            ON product_delivery_tasks(story_id)""",
        """CREATE TABLE IF NOT EXISTS product_delivery_acceptance_criteria (
            id         TEXT PRIMARY KEY,
            story_id   TEXT NOT NULL REFERENCES product_delivery_stories(id) ON DELETE CASCADE,
            text       TEXT NOT NULL,
            satisfied  BOOLEAN NOT NULL DEFAULT FALSE,
            author     TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_ac_story
            ON product_delivery_acceptance_criteria(story_id)""",
        # -----------------------------------------------------------------
        # Feedback intake — the seed for next sprint's grooming.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS product_delivery_feedback_items (
            id               TEXT PRIMARY KEY,
            product_id       TEXT NOT NULL REFERENCES product_delivery_products(id) ON DELETE CASCADE,
            source           TEXT NOT NULL,
            raw_payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
            severity         TEXT NOT NULL DEFAULT 'normal',
            status           TEXT NOT NULL DEFAULT 'open',
            linked_story_id  TEXT REFERENCES product_delivery_stories(id) ON DELETE SET NULL,
            author           TEXT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_feedback_product_status
            ON product_delivery_feedback_items(product_id, status)""",
    ],
    table_names=[
        "product_delivery_acceptance_criteria",
        "product_delivery_tasks",
        "product_delivery_stories",
        "product_delivery_epics",
        "product_delivery_initiatives",
        "product_delivery_feedback_items",
        "product_delivery_products",
    ],
)
