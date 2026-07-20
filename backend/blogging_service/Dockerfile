FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY blogging_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium deps (--with-deps targets Ubuntu; install manually for Debian Trixie)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2t64 libatspi2.0-0 \
    fonts-liberation fonts-unifont \
    && rm -rf /var/lib/apt/lists/* \
    && playwright install chromium

# ── Shared code ──────────────────────────────────────────────────────────────
# Shared env-var parser (leaf, stdlib-only). Imported by llm_service.config,
# shared.postgres.client, and shared.observability.process_health — copy it
# first so those imports resolve.
COPY shared/env /app/shared.env

# Typed env-config helpers (env_bool/env_int/env_float; leaf, stdlib-only).
# Imported by agents.blogging.shared.job_event_bus.
COPY shared/env_config /app/shared.env_config

# Shared per-job SSE event bus (BusState + pure publish/subscribe/reap; leaf).
# Imported by agents.blogging.shared.job_event_bus.
COPY shared/job_event_bus /app/shared.job_event_bus

# Shared SSE streaming helper (framing + per-job stream generators; leaf).
# Imported by agents.blogging.api.main's job-status stream endpoint.
COPY shared/sse /app/shared.sse

# Neutral JSON-salvage engine (stdlib-only leaf; lazy-imports json-repair from
# requirements.txt). Imported by llm_service.clients.ollama to recover
# prose-wrapped/fenced LLM output — copy it before llm_service so that resolves.
COPY shared/llm_recovery /app/shared.llm_recovery

# LLM service (used by all blogging agents)
COPY agents/llm_service /app/llm_service

# Job service HTTP client
COPY agents/job_service_client.py /app/job_service_client.py

# Shared concurrency primitives (BackgroundHeartbeat) imported by job_service_client
COPY shared/concurrency /app/shared.concurrency

# Shared HTTP connection pool (get_pooled_client) imported by job_service_client
COPY shared/http /app/shared.http

# Integration contracts (used by integrations_store)
COPY agents/integrations /app/integrations

# Postgres schema registry (used by blogging lifespan to register its schema)
COPY shared/postgres /app/shared.postgres

# Cross-team user profile — blog_job_store records a best-effort artifact
# association on job creation (`from user_profile import ...`). Depends only on
# shared.postgres (copied above) + psycopg/pydantic (in requirements.txt).
COPY agents/user_profile /app/user_profile

# Shared Temporal helpers (checkpoints/runner/worker) imported by blog_job_store
COPY shared/temporal /app/shared.temporal

# Shared OpenTelemetry helpers (init_otel/instrument_fastapi_app) imported by
# agents.blogging.api.main. Falls through to no-op when the opentelemetry SDK
# packages are not installed, so no extra deps needed in this slim image.
COPY shared/observability /app/shared.observability

# Team FastAPI app factory (create_team_app) imported by agents.blogging.api.main.
# Wraps init_otel + the schema-registering lifespan; depends only on
# shared.observability (above) and a lazy shared.postgres import (below).
COPY shared/app /app/shared.app

# Minimal unified_api modules needed by blogging Medium integration.
# These are lazy-imported by agents/blogging/shared/medium_integration_access.py.
COPY unified_api/__init__.py /app/unified_api/__init__.py
COPY unified_api/integrations_store.py /app/unified_api/integrations_store.py
COPY unified_api/integration_credentials.py /app/unified_api/integration_credentials.py
COPY unified_api/postgres_encrypted_credentials.py /app/unified_api/postgres_encrypted_credentials.py
COPY unified_api/google_browser_login_credentials.py /app/unified_api/google_browser_login_credentials.py
COPY unified_api/medium_browser_login.py /app/unified_api/medium_browser_login.py

# ── Blogging team code ───────────────────────────────────────────────────────
# Copied under /app/agents/blogging (not /app/blogging) so it resolves as the
# agents.blogging.* namespace package, matching the import path used throughout
# backend/agents/blogging/**.
COPY agents/blogging /app/agents/blogging

# ── Entrypoint ───────────────────────────────────────────────────────────────
COPY blogging_service/entrypoint.py /app/entrypoint.py

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /data/agents \
    && chown -R appuser:appuser /data
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
EXPOSE 8090

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD curl -f http://localhost:8090/health || exit 1

CMD ["python", "entrypoint.py"]
