"""Postgres schema for the Agent Cognition Core.

Pure data module — importing it has no side effects. DDL runs when the
unified API lifespan calls ``shared_postgres.register_team_schemas(SCHEMA)``.

The five tables form the durable substrate for the cognition epic:

* ``agent_cognition_events`` — append-only episodic memory.
* ``agent_cognition_summaries`` — calendar-scoped rollups (day/week/month/year).
* ``agent_cognition_rules`` — advisory/enforced rules with lifecycle status.
* ``agent_cognition_rule_proposals`` — the human-in-the-loop review queue.
* ``agent_cognition_runs`` — the invoke idempotency/leasing ledger.

Every row is keyed by ``agent_id``. Three constraints are load-bearing for
later steps' ``ON CONFLICT`` / claim clauses and are asserted by the tests:
the events idempotency triple, the summary period key, and the runs PK.
"""

from __future__ import annotations

from shared_postgres import TeamSchema

SCHEMA: TeamSchema = TeamSchema(
    team="agent_cognition",
    database=None,
    statements=[
        # -----------------------------------------------------------------
        # Episodic memory — append-only events. Idempotent on the writeback
        # key ``(agent_id, source_run_id, source_seq)`` so a retried or
        # duplicated persist is a no-op rather than skewing rollups.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_cognition_events (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            kind            TEXT NOT NULL,
            content         TEXT NOT NULL DEFAULT '',
            data            JSONB NOT NULL DEFAULT '{}'::jsonb,
            salience        DOUBLE PRECISION NOT NULL DEFAULT 0,
            occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source_run_id   TEXT NOT NULL,
            source_seq      INTEGER NOT NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        # ``recorded_at`` is the event's arrival (append) time, distinct from
        # ``occurred_at`` (when it happened). prune_events deletes only events
        # already folded into the day summary (``recorded_at <=
        # summaries.computed_at``), so a late event appended after the summary
        # was computed is never pruned before it can be amended in — even if the
        # pruner races ahead of the stale-marking. Idempotent ALTER back-fills
        # clusters provisioned before this column.
        """ALTER TABLE agent_cognition_events
            ADD COLUMN IF NOT EXISTS recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_cognition_events_writeback
            ON agent_cognition_events(agent_id, source_run_id, source_seq)""",
        """CREATE INDEX IF NOT EXISTS idx_agent_cognition_events_agent_occurred
            ON agent_cognition_events(agent_id, occurred_at DESC)""",
        """CREATE INDEX IF NOT EXISTS idx_agent_cognition_events_agent_kind
            ON agent_cognition_events(agent_id, kind)""",
        # -----------------------------------------------------------------
        # Rollups — one row per closed calendar period. Idempotent upsert on
        # ``(agent_id, scale, period_start)``; ``version`` bumps on recompute
        # and ``stale`` flags a period for re-summarization on late arrival.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_cognition_summaries (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            scale           TEXT NOT NULL,
            period_start    TIMESTAMPTZ NOT NULL,
            period_end      TIMESTAMPTZ NOT NULL,
            summary         TEXT NOT NULL DEFAULT '',
            highlights      JSONB NOT NULL DEFAULT '[]'::jsonb,
            source_count    INTEGER NOT NULL DEFAULT 0,
            covers_through  TIMESTAMPTZ,
            version         INTEGER NOT NULL DEFAULT 1,
            stale           BOOLEAN NOT NULL DEFAULT FALSE,
            events_pruned   BOOLEAN NOT NULL DEFAULT FALSE,
            computed_at     TIMESTAMPTZ,
            stale_since     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        # ``events_pruned`` is the durable recompute-vs-amend marker: the memory
        # store latches it TRUE on a day summary when retention deletes that
        # day's raw events, so a later late arrival is amended onto the existing
        # summary (not recomputed from an incomplete set) even across a restart.
        # ALTER form is idempotent and back-fills clusters provisioned before it.
        """ALTER TABLE agent_cognition_summaries
            ADD COLUMN IF NOT EXISTS events_pruned BOOLEAN NOT NULL DEFAULT FALSE""",
        # ``computed_at`` records when the rollup last (re)computed this summary.
        # prune_events compares it against an event's ``recorded_at`` to delete
        # only events that were folded in (``recorded_at <= computed_at``), so a
        # not-yet-incorporated late arrival is never pruned. Set by
        # upsert_summary; idempotent ALTER back-fills existing clusters (NULL
        # until the next recompute, which conservatively prunes nothing).
        """ALTER TABLE agent_cognition_summaries
            ADD COLUMN IF NOT EXISTS computed_at TIMESTAMPTZ""",
        # ``stale_since`` is the time the most recent late arrival flagged this
        # period stale (refreshed by mark_period_stale). upsert_summary refuses
        # to clear ``stale`` when ``stale_since > computed_at`` — i.e. a slow
        # rollup that read before the late event must not overwrite the stale
        # flag set after its read, which would drop the late event. Idempotent
        # ALTER back-fills existing clusters.
        """ALTER TABLE agent_cognition_summaries
            ADD COLUMN IF NOT EXISTS stale_since TIMESTAMPTZ""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_cognition_summaries_period
            ON agent_cognition_summaries(agent_id, scale, period_start)""",
        # -----------------------------------------------------------------
        # Rules — advisory/enforced guardrails. ``evidence`` carries
        # ``(summary_id, version)`` refs for derived rules; ``needs_review``
        # resurfaces a rule whose evidence later changed.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_cognition_rules (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            text            TEXT NOT NULL,
            mode            TEXT NOT NULL,
            status          TEXT NOT NULL,
            predicate       JSONB NOT NULL DEFAULT '{}'::jsonb,
            rationale       TEXT,
            source          TEXT NOT NULL,
            evidence        JSONB NOT NULL DEFAULT '[]'::jsonb,
            needs_review    BOOLEAN NOT NULL DEFAULT FALSE,
            priority        INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_cognition_rules_agent_status
            ON agent_cognition_rules(agent_id, status)""",
        # -----------------------------------------------------------------
        # Rule proposals — HITL review queue. ``action`` + ``target_rule_id``
        # make the approve path deterministic; ``superseded`` is the system
        # auto-withdraw terminal state (distinct from a human ``rejected``).
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_cognition_rule_proposals (
            id              TEXT PRIMARY KEY,
            agent_id        TEXT NOT NULL,
            action          TEXT NOT NULL,
            target_rule_id  TEXT,
            proposed_rule   JSONB,
            evidence        JSONB NOT NULL DEFAULT '[]'::jsonb,
            stale_evidence  BOOLEAN NOT NULL DEFAULT FALSE,
            status          TEXT NOT NULL DEFAULT 'pending',
            decided_by      TEXT,
            decided_at      TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        """CREATE INDEX IF NOT EXISTS idx_agent_cognition_rule_proposals_agent_status
            ON agent_cognition_rule_proposals(agent_id, status)""",
        # -----------------------------------------------------------------
        # Run ledger — invoke idempotency + leasing. PK ``(agent_id,
        # source_run_id)``; the proxy stores the final envelope on both
        # terminal outcomes (completed/blocked) so retries replay either.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_cognition_runs (
            agent_id          TEXT NOT NULL,
            source_run_id     TEXT NOT NULL,
            status            TEXT NOT NULL,
            request_hash      TEXT NOT NULL,
            response          JSONB,
            lease_expires_at  TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at      TIMESTAMPTZ,
            PRIMARY KEY (agent_id, source_run_id)
        )""",
        # -----------------------------------------------------------------
        # Knowledge-graph ingestion watermarks — one row per agent tracking how
        # far the graph sync worker has consumed this agent's memory into the
        # Graphiti/Neo4j knowledge graph. Two symmetric keyset cursors: events on
        # ``(recorded_at, id)`` (arrival time, mirroring prune_events' fold
        # semantics, so a late event with a fresh ``recorded_at`` re-sorts after
        # the watermark and is picked up on a later pass) and rollup summaries on
        # ``(created_at, id)``. All advances are monotonic — the worker reads the
        # watermark then drains strictly after it, and the scheduler single-flights
        # ingestion per agent, so the cursors never regress.
        # -----------------------------------------------------------------
        """CREATE TABLE IF NOT EXISTS agent_cognition_graph_watermarks (
            agent_id                 TEXT PRIMARY KEY,
            last_event_recorded_at   TIMESTAMPTZ,
            last_event_id            TEXT,
            last_summary_created_at  TIMESTAMPTZ,
            last_summary_id          TEXT,
            ingested_count           BIGINT NOT NULL DEFAULT 0,
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        # Keyset-pagination indexes for the sync worker's delta scans: events by
        # ``(agent_id, recorded_at, id)`` and summaries by ``(agent_id,
        # created_at, id)``, so each scan reads strictly after the stored
        # watermark without walking the whole partition.
        """CREATE INDEX IF NOT EXISTS idx_agent_cognition_events_agent_recorded
            ON agent_cognition_events(agent_id, recorded_at, id)""",
        """CREATE INDEX IF NOT EXISTS idx_agent_cognition_summaries_agent_created
            ON agent_cognition_summaries(agent_id, created_at, id)""",
    ],
    table_names=[
        "agent_cognition_events",
        "agent_cognition_summaries",
        "agent_cognition_rules",
        "agent_cognition_rule_proposals",
        "agent_cognition_runs",
        "agent_cognition_graph_watermarks",
    ],
)
