FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# NOTE: build context must be ../backend (i.e. the parent of job_service/),
# so the Dockerfile can COPY from the shared/ package tree.
COPY job_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared package tree, as an importable `shared` package (PYTHONPATH=/app
# below). Only the leaves this service actually needs are copied:
#   - shared/env: leaf, stdlib-only env-var parser; shared.postgres.client
#     imports it, so it must be on the image or the app fails to import.
#   - shared/postgres: Postgres client + schema registry.
# `shared/__init__.py` must come along too so `import shared.postgres`
# resolves — copying into /app/shared/<name> (not /app/shared.<name>)
# preserves the package structure the code imports as `shared.postgres`.
COPY shared/__init__.py /app/shared/__init__.py
COPY shared/env /app/shared/env
COPY shared/postgres /app/shared/postgres

# Job service source
COPY job_service /app/

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
EXPOSE 8085

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
