"""
Reusable plan patterns for planning agents.

These patterns provide structured templates that planners can select and adapt
instead of generating from scratch, reducing tokens and variance.
"""

PLAN_PATTERNS_LIBRARY = """
**Plan patterns (select and adapt when applicable):**

1. **CRUD API + SPA**: Backend: models/schema → CRUD endpoints → validation → error handling. Frontend: app shell → list/detail forms → API service → routing.

2. **Background worker + queue**: Backend: queue config → worker process → job handlers → retry/error handling. DevOps: container for worker, scaling config.

3. **Feature-flagged rollout**: Backend: feature flag service → config storage → API for flags. Frontend: flag provider → conditional rendering. DevOps: env vars for flags.

4. **Standard CI/CD + observability**: DevOps: Dockerfile → docker-compose → CI (lint/test/build) → CD (deploy). Observability: health checks, logging, metrics endpoints.

5. **Auth flow (JWT)**: Backend: user model → register/login endpoints → JWT middleware → protected routes. Frontend: auth service → login/register pages → auth guard → interceptors.

6. **SLA & Observability**: When open questions involve SLAs (availability, latency, RTO/RPO, incident response): DevOps: define SLOs and error budgets → implement metrics, traces, logs aligned with SLOs → configure alerts with thresholds and escalation policies → document runbooks for incident response and on-call. Backend: add health check endpoints, structured logging, and latency instrumentation.
"""

BACKEND_PATTERN_HINTS = """
When the spec suggests a common pattern, adapt it with GRANULAR tasks (one task per logical unit):
- CRUD entity → backend-{entity}-models, backend-{entity}-crud-endpoints, backend-{entity}-validation (separate tasks)
- API with auth → backend-auth-endpoints, backend-auth-middleware, backend-protected-routes (separate tasks)
- Background job → backend-queue-config, backend-worker-process, backend-job-handlers (separate tasks)
Emit nodes in fixed schema: id, domain, kind, summary, details, user_story, acceptance_criteria. Keep summaries under 80 chars.
"""

FRONTEND_PATTERN_HINTS = """
When the spec suggests a common pattern, adapt it:
- CRUD UI → list component → detail/form component → service → routing
- Auth UI → login/register pages → auth service → guard → interceptors
- Dashboard → layout → sidebar → page components
Emit nodes in fixed schema: id, domain, kind, summary, details, user_story, acceptance_criteria. Keep summaries under 80 chars.
"""
