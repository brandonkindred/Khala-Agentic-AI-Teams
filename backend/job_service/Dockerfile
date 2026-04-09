FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# NOTE: build context must be ../backend (i.e. the parent of job_service/),
# so the Dockerfile can COPY from agents/shared_postgres.
COPY job_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared Postgres client + schema registry
COPY agents/shared_postgres /app/shared_postgres

# Job service source
COPY job_service /app/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
EXPOSE 8085

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
