---
name: Docker Agent Packaging
overview: Create a multi-service Docker image that packages all 6 agent teams with pre-installed tools (Node.js, Angular CLI, Git, Docker CLI), exposes API endpoints for each team, and connects to an external Ollama server on the host machine.
todos:
  - id: dockerfile
    content: |
      Create Dockerfile at project root with multi-stage build: (1) Base stage from python:3.11-slim with apt packages: git, curl, ca-certificates, gnupg, build-essential. (2) Install Docker CLI via official Docker apt repository (docker-ce-cli only, no daemon). (3) Install NVM v0.40.4, Node.js 22.12, and Angular CLI 18 globally via npm. (4) Install supervisor via pip for process management. (5) Copy all Python source: requirements.txt, software_engineering_team/, blogging/, market_research_team/, soc2_compliance_team/, social_media_marketing_team/, api/. (6) pip install all requirements from root, software_engineering_team, and blogging directories. (7) Create non-root user 'agent' with UID 1000, add to docker group for socket access. (8) Copy supervisord.conf and entrypoint.sh, chmod +x entrypoint.sh. (9) EXPOSE ports 8000-8005. (10) Set ENTRYPOINT to entrypoint.sh. (11) Set ENV vars: NVM_DIR, PATH with node/npm, PYTHONUNBUFFERED=1.
    status: completed
  - id: docker-compose
    content: |
      Create docker-compose.yml at project root with service 'khala': (1) build context '.' with Dockerfile. (2) container_name: khala. (3) volumes: /var/run/docker.sock:/var/run/docker.sock (Docker CLI access), ./workspace:/workspace (working directory for agent repos). (4) ports: 8000:8000, 8001:8001, 8002:8002, 8003:8003, 8004:8004, 8005:8005. (5) environment: SW_LLM_PROVIDER=ollama, SW_LLM_BASE_URL=http://host.docker.internal:11434, SW_LLM_MODEL=${SW_LLM_MODEL:-qwen3-coder-next:cloud}, PYTHONUNBUFFERED=1. (6) extra_hosts: host.docker.internal:host-gateway (required for Linux to reach host Ollama). (7) restart: unless-stopped. (8) healthcheck: curl --fail http://localhost:8000/health with interval 30s, timeout 10s, retries 3.
    status: completed
  - id: supervisord
    content: |
      Create supervisord.conf at project root with: (1) [supervisord] section: nodaemon=true, logfile=/var/log/supervisor/supervisord.log, childlogdir=/var/log/supervisor. (2) [program:software_engineering_api]: command=python -m uvicorn software_engineering_team.api.main:app --host 0.0.0.0 --port 8000, directory=/app, autostart=true, autorestart=true, stdout_logfile=/var/log/supervisor/sw_api.log, stderr_logfile=/var/log/supervisor/sw_api_err.log. (3) [program:blogging_api]: command=python -m uvicorn blogging.api.main:app --host 0.0.0.0 --port 8001, similar config. (4) [program:market_research_api]: command=python -m uvicorn market_research_team.api.main:app --host 0.0.0.0 --port 8002. (5) [program:soc2_compliance_api]: command=python -m uvicorn soc2_compliance_team.api.main:app --host 0.0.0.0 --port 8003. (6) [program:social_marketing_api]: command=python -m uvicorn social_media_marketing_team.api.main:app --host 0.0.0.0 --port 8004. (7) [program:blog_research_api]: command=python -m uvicorn api.main:app --host 0.0.0.0 --port 8005. (8) All programs should have environment vars for NVM_DIR and PATH to include node/npm.
    status: completed
  - id: entrypoint
    content: |
      Create entrypoint.sh at project root: (1) #!/bin/bash with set -e for fail-fast. (2) Source NVM: export NVM_DIR and source $NVM_DIR/nvm.sh. (3) Verify tools with version checks: node --version, npm --version, ng version, git --version, docker --version, python --version. (4) Create /var/log/supervisor directory if not exists. (5) Print startup banner with tool versions and listening ports. (6) exec supervisord -c /app/supervisord.conf to replace shell process.
    status: completed
  - id: dockerignore
    content: |
      Create .dockerignore at project root excluding: (1) Git: .git/, .gitignore. (2) Python: __pycache__/, *.pyc, *.pyo, .pytest_cache/, .mypy_cache/, .coverage, htmlcov/, .venv/, venv/, .env, *.egg-info/. (3) Node: node_modules/, dist/, .angular/. (4) IDE: .idea/, .vscode/, *.swp. (5) Docker: Dockerfile, docker-compose.yml, .dockerignore. (6) Test repos: software_engineering_team/test_repo/. (7) Plans: .cursor/. (8) Docs: *.md (except README.md if needed inside container).
    status: completed
  - id: readme-docker
    content: |
      Create docker/README.md with Docker usage documentation: (1) Prerequisites: Docker, Docker Compose, Ollama running on host. (2) Quick start: docker-compose up -d. (3) Environment variables table: SW_LLM_PROVIDER, SW_LLM_BASE_URL, SW_LLM_MODEL, per-agent model overrides. (4) Port mapping table for all 6 APIs with example curl commands. (5) Volume mounts explanation: workspace for agent output, docker.sock for Docker CLI. (6) Troubleshooting: Ollama connection issues, Docker socket permissions, health check failures. (7) Building custom image: docker build -t khala . (8) Security considerations: Docker socket access implications.
    status: completed
  - id: verify-build
    content: |
      Verify the Docker build works: (1) Run docker build -t khala . and ensure it completes without errors. (2) Run docker-compose up -d and verify container starts. (3) Test health endpoints: curl localhost:8000/health through 8005/health. (4) Verify tools inside container: docker exec khala node --version, ng version, git --version, docker --version. (5) Test a simple API call to /run-team or similar endpoint.
    status: completed
isProject: false
---

# Docker Agent Environment Packaging

## Architecture Overview

```mermaid
flowchart TB
    subgraph HostMachine [Host Machine]
        Ollama["Ollama Server :11434"]
        DockerSocket["Docker Socket"]
    end
    
    subgraph AgentContainer [Agent Container]
        subgraph APIs [API Services]
            SW["SW Engineering :8000"]
            Blog["Blogging :8001"]
            Market["Market Research :8002"]
            SOC2["SOC2 Compliance :8003"]
            Social["Social Marketing :8004"]
            Root["Blog Research :8005"]
        end
        
        subgraph Tools [Pre-installed Tools]
            NodeJS["Node.js 22 + NVM"]
            Angular["Angular CLI 18"]
            Python["Python 3.11"]
            GitTool["Git"]
            DockerCLI["Docker CLI"]
        end
        
        Supervisor["Supervisord"]
    end
    
    AgentContainer -->|"LLM API"| Ollama
    AgentContainer -->|"docker.sock"| DockerSocket
```



## Files to Create

### 1. Dockerfile (`[Dockerfile](Dockerfile)`)

Multi-stage build with:

- **Base stage**: Python 3.11-slim with system dependencies
- **Node stage**: NVM + Node.js 22.12 + Angular CLI 18
- **Final stage**: Combined runtime with all tools

Key components:

- Python 3.11 with pip dependencies from all teams
- NVM with Node.js 22.12 (Angular CLI requirement)
- Angular CLI 18 globally installed
- Git for repository operations
- Docker CLI for DevOps agent operations (no daemon - uses host socket)
- Non-root user `agent` for security

### 2. docker-compose.yml (`[docker-compose.yml](docker-compose.yml)`)

Service definition with:

- Volume mount for `/var/run/docker.sock` (Docker CLI access)
- Volume mount for workspace directory
- Port mappings for all 6 APIs (8000-8005)
- Environment variables for Ollama connection (`SW_LLM_BASE_URL=http://host.docker.internal:11434`)
- `extra_hosts` for `host.docker.internal` on Linux

### 3. Supervisor Configuration (`[supervisord.conf](supervisord.conf)`)

Process manager to run all 6 API servers:

- `software_engineering_api` on port 8000
- `blogging_api` on port 8001
- `market_research_api` on port 8002
- `soc2_compliance_api` on port 8003
- `social_marketing_api` on port 8004
- `blog_research_api` on port 8005

### 4. Entrypoint Script (`[entrypoint.sh](entrypoint.sh)`)

Startup script that:

- Sources NVM for Node.js availability
- Verifies tool availability (node, ng, git, docker)
- Starts supervisord

### 5. .dockerignore (`[.dockerignore](.dockerignore)`)

Excludes unnecessary files from build context

## Key Design Decisions

### Docker-in-Docker Approach

Using **Docker socket binding** (mounting `/var/run/docker.sock`) rather than true DinD because:

- The DevOps agent only needs to run `docker build` for verification
- Simpler setup without `--privileged` flag
- Lower overhead than running a nested daemon

Security consideration: The socket mount gives container access to host Docker daemon. For production, consider:

- Running on a dedicated CI/build host
- Using Docker's allowlist features for trusted images
- Implementing command restrictions

### Port Allocation


| Port | Team                 | Endpoint Prefix                             |
| ---- | -------------------- | ------------------------------------------- |
| 8000 | Software Engineering | `/run-team`, `/clarification`, `/execution` |
| 8001 | Blogging             | `/research-and-review`, `/full-pipeline`    |
| 8002 | Market Research      | `/market-research`                          |
| 8003 | SOC2 Compliance      | `/soc2-audit`                               |
| 8004 | Social Marketing     | `/social-marketing`                         |
| 8005 | Blog Research (root) | `/research-and-review`                      |


### Ollama Connection

The container connects to the host's Ollama server using:

- `host.docker.internal:11434` (Docker Desktop on macOS/Windows)
- `extra_hosts: ["host.docker.internal:host-gateway"]` for Linux

Environment variable `SW_LLM_BASE_URL` configures the connection.

## Usage

```bash
# Build the image
docker build -t khala .

# Run with docker-compose (recommended)
docker-compose up -d

# Or run directly
docker run -d \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v $(pwd)/workspace:/workspace \
  -p 8000-8005:8000-8005 \
  -e SW_LLM_BASE_URL=http://host.docker.internal:11434 \
  --add-host=host.docker.internal:host-gateway \
  khala

# Check health
curl http://localhost:8000/health
curl http://localhost:8001/health
# ... etc
```

## Dependencies Consolidated

All Python dependencies from:

- `[requirements.txt](requirements.txt)` (root)
- `[software_engineering_team/requirements.txt](software_engineering_team/requirements.txt)`
- `[blogging/requirements.txt](blogging/requirements.txt)`

Will be merged into a single consolidated requirements file in the Docker build.