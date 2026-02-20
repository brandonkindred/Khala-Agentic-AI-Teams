# Multi-stage Dockerfile for strands-agents
# Packages all 6 agent teams with pre-installed tools

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
    && npm install -g @angular/cli@18 \
    && nvm alias default ${NODE_VERSION}

# ---------------------------------------------------------------------------
# Stage 3: Final image
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
COPY requirements.txt /app/requirements.txt
COPY software_engineering_team/requirements.txt /app/software_engineering_team/requirements.txt
COPY blogging/requirements.txt /app/blogging/requirements.txt

# Merge and install - root has most deps, add any unique from other teams
RUN pip install --no-cache-dir -r /app/requirements.txt \
    && pip install --no-cache-dir -r /app/software_engineering_team/requirements.txt \
    && pip install --no-cache-dir -r /app/blogging/requirements.txt

# Copy application source
COPY software_engineering_team /app/software_engineering_team
COPY blogging /app/blogging
COPY market_research_team /app/market_research_team
COPY soc2_compliance_team /app/soc2_compliance_team
COPY social_media_marketing_team /app/social_media_marketing_team
COPY api /app/api

# Create docker group and ensure socket directory exists for DinD
RUN groupadd -g 999 docker 2>/dev/null || true \
    && useradd -m -u 1000 -G docker -s /bin/bash agent 2>/dev/null || true \
    && mkdir -p /var/run

# Copy config files
COPY supervisord.conf /app/supervisord.conf
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create log directory and workspace
RUN mkdir -p /var/log/supervisor /workspace

# Copy spec file into /app (path via build arg, default: docker/default_initial_spec.md)
ARG SPEC_FILE=docker/default_initial_spec.md
COPY ${SPEC_FILE} /app/initial_spec.md

EXPOSE 8000 8001 8002 8003 8004 8005

ENTRYPOINT ["/app/entrypoint.sh"]
