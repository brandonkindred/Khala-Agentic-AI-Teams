FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install shared Python deps
COPY team_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# All agent code + shared modules (llm_service, job_service_client, integrations).
# Copying the entire agents/ tree keeps import paths identical to the monolith
# and avoids needing per-team Dockerfiles.
COPY agents/ /app/agents/

# Unified API modules needed by some teams (Medium integration, credential store)
COPY unified_api/__init__.py /app/unified_api/__init__.py
COPY unified_api/integrations_store.py /app/unified_api/integrations_store.py
COPY unified_api/integration_credentials.py /app/unified_api/integration_credentials.py
COPY unified_api/postgres_encrypted_credentials.py /app/unified_api/postgres_encrypted_credentials.py
COPY unified_api/google_browser_login_credentials.py /app/unified_api/google_browser_login_credentials.py
COPY unified_api/medium_browser_login.py /app/unified_api/medium_browser_login.py

# Generic entrypoint (configured via env vars at runtime)
COPY team_service/entrypoint.py /app/entrypoint.py

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /data/agents \
    && chown -R appuser:appuser /data
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/agents:/app/agents/blogging:/app

ARG TEAM_PORT=8090
ENV TEAM_PORT=${TEAM_PORT}
EXPOSE ${TEAM_PORT}

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD curl -f http://localhost:${TEAM_PORT}/health || exit 1

CMD ["python", "entrypoint.py"]
