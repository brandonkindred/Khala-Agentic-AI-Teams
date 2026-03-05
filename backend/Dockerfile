# Unified API server image. Matches backend Makefile: same deps, same run as `make deploy`.
# Build from repo root: docker build -f backend/Dockerfile backend
# Or from backend: docker build -t strands-unified-api .

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

EXPOSE 8080

# Same as make deploy (host 0.0.0.0, port 8080, workers 4)
CMD ["python", "run_unified_api.py", "--host", "0.0.0.0", "--port", "8080", "--workers", "4", "--log-level", "warning"]
