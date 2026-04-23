# syntax=docker/dockerfile:1.7
# Multi-stage Dockerfile for khala
# This is the agent-provisioning supervisor image: bundles Docker Engine
# (DinD), PostgreSQL, nginx, Node/Angular, and every enabled agent team so
# the agent_provisioning_team can spawn isolated per-agent sandbox
# containers INSIDE this container (see agent_provisioning_team/tool_agents/
# docker_provisioner.py). This is NOT the production runtime — for that
# see backend/team_service/Dockerfile and backend/blogging_service/Dockerfile.
#
# Build speed notes:
#   * BuildKit cache mounts (--mount=type=cache) keep apt/pip caches across
#     builds without bloating the final image.
#   * docker-clean is removed from apt config so cache mounts actually work
#     (debian base images ship a hook that wipes /var/cache/apt after install).

# ---------------------------------------------------------------------------
# Stage 1: Base with system dependencies and full Docker Engine
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Let BuildKit cache mounts retain downloaded packages between builds.
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    lsb-release

# Install full Docker Engine (daemon + CLI) for Docker-in-Docker
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian bookworm stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io

# Install PostgreSQL 15 and nginx
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-15 \
        postgresql-contrib-15 \
        nginx

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

# Set working directory
WORKDIR /app

# Requirements files for every team that ships one. Copied BEFORE the source
# tree so the pip layer only busts when a requirements file actually changes.
COPY agents/requirements.txt /app/requirements.txt
COPY agents/software_engineering_team/requirements.txt /app/software_engineering_team/requirements.txt
COPY agents/blogging/requirements.txt /app/blogging/requirements.txt
COPY agents/personal_assistant_team/requirements.txt /app/personal_assistant_team/requirements.txt
COPY agents/agent_provisioning_team/requirements.txt /app/agent_provisioning_team/requirements.txt

# One pip invocation with a BuildKit cache mount — wheel cache survives
# across builds instead of being redownloaded every time. supervisor is
# installed here too so it shares the same pip cache layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        supervisor \
        -r /app/requirements.txt \
        -r /app/software_engineering_team/requirements.txt \
        -r /app/blogging/requirements.txt \
        -r /app/personal_assistant_team/requirements.txt \
        -r /app/agent_provisioning_team/requirements.txt

# Copy every team and shared module (llm_service, shared_postgres,
# shared_temporal, integrations, etc.) into /app/ so PYTHONPATH=/app can
# import them by package name (e.g. `software_engineering_team.api.main`).
# This covers all 20 enabled teams in unified_api/config.py TEAM_CONFIGS
# plus the shared infrastructure they depend on.
COPY agents /app/

# Copy the built Angular UI
COPY --from=ui-build /build/dist/user-interface/browser /app/ui

# Create docker group and ensure socket directory exists for DinD
RUN groupadd -g 999 docker 2>/dev/null || true \
    && useradd -m -u 1000 -G docker -s /bin/bash agent 2>/dev/null || true \
    && mkdir -p /var/run

# supervisord.conf and entrypoint.sh already landed at /app/ via the wholesale
# `COPY agents /app/` above. Only nginx.conf needs relocation to /etc/nginx/.
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
