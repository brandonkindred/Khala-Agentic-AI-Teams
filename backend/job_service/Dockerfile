FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# NOTE: build context must be ../backend (i.e. the parent of job_service/),
# so the Dockerfile can COPY from agents/shared_postgres and agents/shared_env.
COPY job_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared env-var parser (leaf, stdlib-only). shared_postgres.client imports it,
# so it must be on the image or the app fails to import at startup.
COPY agents/shared_env /app/shared_env

# Shared Postgres client + schema registry
COPY agents/shared_postgres /app/shared_postgres

# Job service source
COPY job_service /app/

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
EXPOSE 8085

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
