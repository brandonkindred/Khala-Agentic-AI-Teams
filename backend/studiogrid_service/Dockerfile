FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install shared Python deps
COPY team_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# All agent code + shared modules
COPY agents/ /app/agents/

# StudioGrid lives outside agents/ — copy it separately
COPY studiogrid/ /app/studiogrid/

# Unified API modules needed by some integrations
COPY unified_api/__init__.py /app/unified_api/__init__.py
COPY unified_api/integrations_store.py /app/unified_api/integrations_store.py
COPY unified_api/integration_credentials.py /app/unified_api/integration_credentials.py
COPY unified_api/postgres_encrypted_credentials.py /app/unified_api/postgres_encrypted_credentials.py
COPY unified_api/google_browser_login_credentials.py /app/unified_api/google_browser_login_credentials.py
COPY unified_api/medium_browser_login.py /app/unified_api/medium_browser_login.py

# Generic entrypoint
COPY team_service/entrypoint.py /app/entrypoint.py

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/agents:/app:/app/studiogrid/src

ARG TEAM_PORT=8104
ENV TEAM_PORT=${TEAM_PORT}
EXPOSE ${TEAM_PORT}

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD curl -f http://localhost:${TEAM_PORT}/health || exit 1

CMD ["python", "entrypoint.py"]
