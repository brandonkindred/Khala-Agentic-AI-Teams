"""Postgres schema for the Product Delivery team.

Pure data module — importing it has no side effects. DDL runs when the
unified API lifespan calls
``shared_postgres.register_team_schemas(SCHEMA)``.

Schema shape (Phase 1 + Phase 2 of issue #243):

    products
      └── initiatives
            └── epics
                  └── stories
                        ├── tasks
                        └── acceptance_criteria
    feedback_items     (optionally linked to a story; sprint_id added in #371
                        so Integration / DevOps / QA failures auto-promoted by
                        the ReleaseManagerAgent stay tagged with the sprint
                        they originated from)
    sprints            (Phase 2 — sprint cadence on top of the backlog)
      └── sprint_stories  (M:N planned-into-sprint join)
      └── releases        (Phase 2 — table only; ReleaseManagerAgent ships in #371)

Phase 2 additions are pure ``CREATE TABLE IF NOT EXISTS`` (no destructive
migration). Releases ship the table in Phase 2 so #371 can land routes +
the agent without re-touching this module. Phase 3 (#371) appends one
``ALTER TABLE … ADD COLUMN IF NOT EXISTS sprint_id`` to the feedback
table — idempotent, so re-running on existing deployments leaves
``sprint_id NULL`` for legacy rows.
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
        # -----------------------------------------------------------------
        # Sprints — cadence layer. A sprint scopes a planned slice of the
        # backlog into a time-boxed iteration. Phase 2 of #243.
        #
        # NOTE: the ``ALTER TABLE`` adding ``sprint_id`` to feedback_items
        # is appended at the end of this list so the referenced
        # ``product_delivery_sprints`` table already exists when DDL runs.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS product_delivery_sprints (
            id               TEXT PRIMARY KEY,
            product_id       TEXT NOT NULL REFERENCES product_delivery_products(id) ON DELETE CASCADE,
            name             TEXT NOT NULL,
            capacity_points  DOUBLE PRECISION NOT NULL DEFAULT 0,
            starts_at        TIMESTAMPTZ,
            ends_at          TIMESTAMPTZ,
            status           TEXT NOT NULL DEFAULT 'planned',
            author           TEXT NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_sprints_product
            ON product_delivery_sprints(product_id)""",
        # -----------------------------------------------------------------
        # Releases — table only; routes + ReleaseManagerAgent ship in #371.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS product_delivery_releases (
            id          TEXT PRIMARY KEY,
            sprint_id   TEXT NOT NULL REFERENCES product_delivery_sprints(id) ON DELETE CASCADE,
            version     TEXT NOT NULL,
            notes_path  TEXT,
            shipped_at  TIMESTAMPTZ,
            author      TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_pd_releases_sprint
            ON product_delivery_releases(sprint_id)""",
        # Phase 3 review (#371 / PR #424 Codex P2): a release version is
        # the on-disk filename for ``plan/releases/<version>.md`` and
        # the audit key for the row. Without this UNIQUE index a
        # concurrent ship (or a manual ``POST /releases`` that reuses
        # an existing version) would create a second row pointing at
        # the same notes path, silently rewriting historical release
        # notes. The constraint scopes to ``sprint_id`` because two
        # different sprints can legitimately ship the same version
        # string (e.g. "2026-05-02" on different products / sprints).
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_pd_releases_sprint_version
            ON product_delivery_releases(sprint_id, version)""",
        # -----------------------------------------------------------------
        # Sprint ↔ Story join. A story can be planned into a sprint without
        # losing its place in the backlog hierarchy. Composite PK is the
        # (sprint_id, story_id) pair — `add_story_to_sprint` is idempotent
        # via `ON CONFLICT DO NOTHING`.
        #
        # `UNIQUE(story_id)` enforces the planner's "one-sprint-per-story"
        # invariant at the schema layer (Codex review on PR #396).
        # ``select_sprint_scope`` filters candidates with ``NOT EXISTS``,
        # but two concurrent planners could both pass that check and try
        # to plant the same story into different sprints — the unique
        # constraint closes that race window. Reusing the index for the
        # reverse-direction "is this story already planned anywhere?"
        # lookup that ``select_sprint_scope`` and the SE pipeline rely on.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS product_delivery_sprint_stories (
            sprint_id   TEXT NOT NULL REFERENCES product_delivery_sprints(id) ON DELETE CASCADE,
            story_id    TEXT NOT NULL REFERENCES product_delivery_stories(id) ON DELETE CASCADE,
            planned_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (sprint_id, story_id)
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_pd_sprint_stories_story
            ON product_delivery_sprint_stories(story_id)""",
        # -----------------------------------------------------------------
        # Phase 3 (#371): tag feedback items with the originating sprint so
        # the ReleaseManagerAgent's auto-feedback intake (Integration /
        # DevOps / QA failures during a sprint run) can be filtered for
        # the next groom. ``ON DELETE SET NULL`` keeps the feedback row
        # alive even when the sprint is GC'd — its ``raw_payload`` still
        # carries enough context for triage. Idempotent: existing rows
        # get ``NULL`` on first apply, and the column is unchanged on
        # subsequent applies.
        # -----------------------------------------------------------------
        """ALTER TABLE IF EXISTS product_delivery_feedback_items
            ADD COLUMN IF NOT EXISTS sprint_id TEXT
            REFERENCES product_delivery_sprints(id) ON DELETE SET NULL""",
        """CREATE INDEX IF NOT EXISTS idx_pd_feedback_sprint
            ON product_delivery_feedback_items(sprint_id)""",
    ],
    table_names=[
        "product_delivery_sprint_stories",
        "product_delivery_releases",
        "product_delivery_sprints",
        "product_delivery_acceptance_criteria",
        "product_delivery_tasks",
        "product_delivery_stories",
        "product_delivery_epics",
        "product_delivery_initiatives",
        "product_delivery_feedback_items",
        "product_delivery_products",
    ],
)
