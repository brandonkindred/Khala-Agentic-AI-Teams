# Product Delivery

Persistent product backlog + grooming, sprint planning, and release
management. Recommendation #1 from the Principal-Engineer SDLC review,
tracked end-to-end in issue
[#243](https://github.com/brandonkindred/khala-agentic-ai-teams/issues/243)
("SE team: persistent Product Delivery Loop").

The team has shipped in three phases:

| Phase | PR / Issue | What landed |
|-------|------------|-------------|
| 1     | [#369](https://github.com/brandonkindred/khala-agentic-ai-teams/pull/369) | Backlog tables, `ProductDeliveryStore`, `ProductOwnerAgent`, CRUD + `/groom` + `/feedback` routes. |
| 2     | [#396](https://github.com/brandonkindred/khala-agentic-ai-teams/pull/396) | `sprints` / `releases` / `sprint_stories` tables, `SprintPlannerAgent`, `_load_requirements_from_sprint` in the SE orchestrator, `POST /api/software-engineering/run-team` learns `{sprint_id}`. |
| 3     | [#371](https://github.com/brandonkindred/khala-agentic-ai-teams/issues/371) | `ReleaseManagerAgent`, release-notes generation reusing `technical_writers/release_notes_agent`, SE Integration-phase hook, auto-feedback intake tagged with `sprint_id`, `POST /releases` + `GET /releases?product_id=…`. |

## Phase 3 — releases & auto-feedback

When the SE pipeline runs with `{sprint_id}` and Integration completes,
the orchestrator calls `_maybe_ship_sprint_release` (`orchestrator.py`).
If every planned story has reached a terminal status the
`ReleaseManagerAgent`:

1. Composes a markdown release note via
   `software_engineering_team/technical_writers/release_notes_agent/`
   (a sibling of `documentation_agent`, sharing its Strands
   `documentation` model). The writer has a deterministic fallback
   body so the release event is observable even when the LLM is down.
2. Writes `plan/releases/<version>.md` (`<version>` defaults to today's
   UTC date `YYYY-MM-DD`, with `-N` suffix on collisions).
3. Records a `product_delivery_releases` row with `notes_path` and
   `shipped_at`.
4. Promotes Integration / DevOps / QA failures into
   `product_delivery_feedback_items`, each tagged with `sprint_id` so
   the next `POST /groom` can scope candidate inputs to "what this
   sprint surfaced".

Failures are non-fatal — the hook wraps the agent in `try/except` and,
on agent failure, opens a `release-manager-error` feedback item with
the exception text + `job_id` so the next grooming sees the gap.

## What's deferred to follow-up issues

- Agent Console "Backlog", "Sprints", and "Releases" tabs (Angular).
- `ARCHITECTURE.md` "Product Delivery Loop" section.
- Versioning policy beyond date-stamps (semver vs date — issue #371
  explicitly defers this).
- Temporal-mode plumbing for `sprint_id` runs (Phase 2 raises a 400
  in Temporal mode; same contract here).

## Schema

```
products
  └── initiatives
        └── epics
              └── stories ── tasks
                          ── acceptance_criteria
feedback_items   (linked_story_id NULL when not yet triaged;
                  sprint_id NULL for external feedback,
                  set by ReleaseManagerAgent for SE failures)
sprints           (Phase 2)
  └── sprint_stories  (M:N planned-into-sprint join, UNIQUE(story_id))
  └── releases        (Phase 2 table; ReleaseManagerAgent writes here)
```

Every row carries:

- `id TEXT PRIMARY KEY` — UUID4 hex assigned by the store.
- `author TEXT NOT NULL` — handle from
  `agent_console.author.resolve_author()`. When real auth lands we can
  migrate both `agent_console` and `product_delivery` rows to user ids
  in a single pass.
- `created_at` / `updated_at TIMESTAMPTZ`.

`status` is a free-form `TEXT` column (no Postgres `ENUM`) so adding a
state like `"in_sprint"` later is a no-op.

## API surface

```
POST   /api/product-delivery/products
GET    /api/product-delivery/products
GET    /api/product-delivery/products/{id}/backlog          (nested tree)

POST   /api/product-delivery/initiatives
POST   /api/product-delivery/epics
POST   /api/product-delivery/stories
POST   /api/product-delivery/tasks
POST   /api/product-delivery/acceptance-criteria

PATCH  /api/product-delivery/{kind}/{id}/status
PATCH  /api/product-delivery/{kind}/{id}/scores

POST   /api/product-delivery/groom                          (WSJF or RICE)
POST   /api/product-delivery/feedback                        (accepts sprint_id, #371)
GET    /api/product-delivery/feedback?product_id=…&status=open

POST   /api/product-delivery/sprints                         (Phase 2)
POST   /api/product-delivery/sprints/{id}/plan               (Phase 2)
GET    /api/product-delivery/sprints/{id}                    (Phase 2)

POST   /api/product-delivery/releases                        (Phase 3 / #371)
GET    /api/product-delivery/releases?product_id=…           (Phase 3 / #371)
```

## Local smoke test

```bash
docker compose -f docker/docker-compose.yml up -d postgres job-service
export POSTGRES_HOST=localhost POSTGRES_PORT=5432 \
       POSTGRES_USER=postgres POSTGRES_PASSWORD=postgres POSTGRES_DB=postgres \
       JOB_SERVICE_URL=http://localhost:8085
cd backend && make run

curl -sX POST localhost:8080/api/product-delivery/products \
  -H 'content-type: application/json' \
  -d '{"name":"Demo","description":"smoke","vision":"ship it"}'

# Then create initiative → epic → story → groom.
psql -h localhost -U postgres -c "\dt product_delivery_*"
```

## Testing

- `tests/test_scoring.py` — pure-function unit tests for WSJF / RICE.
- `tests/test_product_owner_agent.py` — agent with stubbed LLM client.
- `tests/test_sprint_planner_agent.py` — Phase 2 thin shell over
  `select_sprint_scope`.
- `tests/test_release_manager_agent.py` — Phase 3 release-shipping +
  failure promotion + version collision handling.
- `tests/test_api.py` — FastAPI routes with the store overridden via
  `app.dependency_overrides`-style monkeypatch on `get_store`.
- `tests/test_store.py` — integration tests against a live Postgres,
  auto-skipped when `POSTGRES_HOST` is unset (matches the agent_console
  pattern).
- `software_engineering_team/tests/test_release_hook.py` — covers
  `_maybe_ship_sprint_release` (no-op on non-sprint runs, skip on open
  stories, ship on completion, non-fatal on agent failure).
