# Multi-stage Dockerfile for khala
# Packages all 6 agent teams with pre-installed tools, UI, PostgreSQL, and nginx

# ---------------------------------------------------------------------------
# Stage 1: Base with system dependencies and full Docker Engine
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Install full Docker Engine (daemon + CLI) for Docker-in-Docker
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io \
    && rm -rf /var/lib/apt/lists/*

# Install PostgreSQL 15 and nginx
RUN echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-15 \
        postgresql-contrib-15 \
        nginx \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Stage 2: Node.js and Angular CLI via NVM
# ---------------------------------------------------------------------------
FROM base AS node-stage

ENV NVM_DIR=/usr/local/nvm
ENV NVM_VERSION=0.40.4
ENV NODE_VERSION=22.12

RUN mkdir -p "$NVM_DIR" \
    && curl -o- "https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh" | bash \
    && . "$NVM_DIR/nvm.sh" \
    && nvm install ${NODE_VERSION} \
    && nvm use ${NODE_VERSION} \
    && npm install -g @angular/cli@19 \
    && nvm alias default ${NODE_VERSION}

# ---------------------------------------------------------------------------
# Stage 3: Build Angular UI
# ---------------------------------------------------------------------------
FROM node-stage AS ui-build

WORKDIR /build

# Copy package files first for better caching
COPY user-interface/package.json user-interface/package-lock.json ./

# Install dependencies
ENV NVM_DIR=/usr/local/nvm
ENV NODE_VERSION=22.12
ENV PATH="${NVM_DIR}/versions/node/v${NODE_VERSION}/bin:${PATH}"

RUN npm ci

# Copy the rest of the UI source and build
COPY user-interface/ ./

RUN npm run build -- --configuration=production

# ---------------------------------------------------------------------------
# Stage 4: Final image
# ---------------------------------------------------------------------------
FROM base AS final

# Copy NVM and Node from node-stage
COPY --from=node-stage /usr/local/nvm /usr/local/nvm

ENV NVM_DIR=/usr/local/nvm
ENV NODE_VERSION=22.12
ENV PATH="${NVM_DIR}/versions/node/v${NODE_VERSION}/bin:${PATH}"

# Install supervisor for process management
RUN pip install --no-cache-dir supervisor

# Set working directory
WORKDIR /app

# Copy consolidated requirements and install Python dependencies
COPY agents/requirements.txt /app/requirements.txt
COPY agents/software_engineering_team/requirements.txt /app/software_engineering_team/requirements.txt
COPY agents/blogging/requirements.txt /app/blogging/requirements.txt

# Merge and install - root has most deps, add any unique from other teams
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir -r /app/software_engineering_team/requirements.txt \
    && pip install --no-cache-dir -r /app/blogging/requirements.txt

# Copy application source
COPY agents/software_engineering_team /app/software_engineering_team
COPY agents/blogging /app/blogging
COPY agents/market_research_team /app/market_research_team
COPY agents/soc2_compliance_team /app/soc2_compliance_team
COPY agents/social_media_marketing_team /app/social_media_marketing_team
COPY agents/api /app/api

# Copy the built Angular UI
COPY --from=ui-build /build/dist/user-interface/browser /app/ui

# Create docker group and ensure socket directory exists for DinD
RUN groupadd -g 999 docker 2>/dev/null || true \
    && useradd -m -u 1000 -G docker -s /bin/bash agent 2>/dev/null || true \
    && mkdir -p /var/run

# Copy config files
COPY agents/supervisord.conf /app/supervisord.conf
COPY agents/entrypoint.sh /app/entrypoint.sh
COPY agents/nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /app/entrypoint.sh

# Create log directory, workspace, and PostgreSQL directories
RUN mkdir -p /var/log/supervisor /workspace /var/lib/postgresql/data /run/postgresql \
    && chown -R postgres:postgres /var/lib/postgresql /run/postgresql

# Copy spec file into /app (path via build arg, default: docker/default_initial_spec.md)
ARG SPEC_FILE=agents/docker/default_initial_spec.md
COPY ${SPEC_FILE} /app/initial_spec.md

# Expose ports: nginx (80), APIs (8000-8005), PostgreSQL (5432)
EXPOSE 80 8000 8001 8002 8003 8004 8005 5432

ENTRYPOINT ["/app/entrypoint.sh"]
