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
# Platform shared packages (shared.env, shared.env_config, shared.job_event_bus,
# shared.sse, shared.llm_recovery, shared.concurrency, shared.http,
# shared.postgres, shared.temporal, shared.observability, shared.app, …).
COPY shared/ /app/shared/

# LLM service (used by all blogging agents)
COPY agents/llm_service /app/llm_service

# Job service HTTP client
COPY agents/job_service_client.py /app/job_service_client.py

# Cross-team user profile — blog_job_store records a best-effort artifact
# association on job creation (`from user_profile import ...`). Depends only on
# shared.postgres (copied above) + psycopg/pydantic (in requirements.txt).
COPY agents/user_profile /app/user_profile

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
