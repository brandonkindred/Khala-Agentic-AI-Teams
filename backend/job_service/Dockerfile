FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8085

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8085"]
