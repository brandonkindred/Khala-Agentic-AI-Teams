# Unified API server image. Matches backend Makefile: same deps, same run as `make deploy`.
# Build from repo root: docker build -f backend/Dockerfile backend
# Or from backend: docker build -t khala-unified-api .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

# For healthcheck in docker-compose
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/app:/app/agents

WORKDIR /app

# Install deps (same as make install: agents/requirements.txt)
COPY agents/requirements.txt /app/agents/requirements.txt
RUN pip install --no-cache-dir -r /app/agents/requirements.txt

# Application: unified API + all agent teams
COPY agents /app/agents
COPY unified_api /app/unified_api
COPY run_unified_api.py /app/

# Generate PA credential encryption key at build time so it persists across container runs
RUN python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > /app/pa_credential_key
ENV PA_CREDENTIAL_KEY_FILE=/app/pa_credential_key

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Same as make deploy (host 0.0.0.0, port 8080, workers 4)
CMD ["python", "run_unified_api.py", "--host", "0.0.0.0", "--port", "8080", "--workers", "4", "--log-level", "warning"]
