# Product Delivery (Phase 1)

Persistent product backlog + Product Owner grooming. Phase 1 of issue
[#243](https://github.com/brandonkindred/khala-agentic-ai-teams/issues/243)
("SE team: persistent Product Delivery Loop"), recommendation #1 from
the Principal-Engineer SDLC review.

## What lands in this PR

- Postgres schema (`product_delivery_*` tables) registered via
  `shared_postgres` Pattern B at unified-API startup.
- `ProductDeliveryStore` — stateless DAL mirroring `agent_console.store`.
- `ProductOwnerAgent` — ranks stories with **WSJF** or **RICE**. The
  agent only asks the LLM for *scoring inputs*; the score itself is
  computed by the deterministic functions in `scoring.py`, so a
  re-groom over the same backlog produces stable, auditable numbers.
- Routes under `/api/product-delivery` (mounted in-process by the
  unified API; this team is **not** a proxy team).

## What's deferred to follow-up issues

- `sprints` + `releases` tables and the sprint-planner phase that gates
  the existing SE Discovery → Design → Execution → Integration pipeline
  (so `POST /api/software-engineering/run-team` can accept `{sprint_id}`
  instead of a fresh spec every time).
- `ReleaseManagerAgent` + release-notes generation hooked into
  Integration.
- Agent Console "Backlog" and "Sprints" tabs (Angular).
- `ARCHITECTURE.md` "Product Delivery Loop" section (lands with the SE
  pipeline integration above).

## Schema

```
products
  └── initiatives
        └── epics
              └── stories ── tasks
                          ── acceptance_criteria
feedback_items   (linked_story_id NULL when not yet triaged)
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
POST   /api/product-delivery/feedback
GET    /api/product-delivery/feedback?product_id=…&status=open
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
- `tests/test_api.py` — FastAPI routes with the store overridden via
  `app.dependency_overrides`-style monkeypatch on `get_store`.
- `tests/test_store.py` — integration tests against a live Postgres,
  auto-skipped when `POSTGRES_HOST` is unset (matches the agent_console
  pattern).
