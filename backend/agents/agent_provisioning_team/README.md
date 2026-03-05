# Agent Provisioning Team

A swarm of agents that provisions sandboxed Docker environments with configurable tool accounts for AI agents, following an employee-onboarding model with least-privilege access and comprehensive onboarding documentation.

## Overview

The Agent Provisioning Team automates the process of setting up development environments for AI agents. Like onboarding a new employee at a company, it provisions:

- **Sandboxed Docker containers** - Isolated execution environments
- **Tool accounts** - PostgreSQL databases, Redis caches, Git repos
- **Secure credentials** - Auto-generated passwords and tokens
- **Access controls** - Least-privilege permissions per tool
- **Onboarding documentation** - Getting-started guides and environment info

## Architecture

The team uses a **phase-based workflow** with 6 sequential phases:

```
1. SETUP              → Create Docker container
2. CREDENTIAL_GEN     → Generate passwords/tokens
3. ACCOUNT_PROVISION  → Create accounts in tools
4. ACCESS_AUDIT       → Verify least-privilege
5. DOCUMENTATION      → Generate onboarding docs
6. DELIVER            → Finalize and return results
```

Progress is tracked via a file-based job store and exposed through REST API endpoints.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/provision` | Start provisioning job |
| GET | `/provision/status/{job_id}` | Get job status with phase progress |
| GET | `/provision/jobs` | List all provisioning jobs |
| GET | `/environments` | List all provisioned agents |
| GET | `/environments/{agent_id}` | Get agent environment status |
| DELETE | `/environments/{agent_id}` | Deprovision an agent |

### Start Provisioning

```bash
curl -X POST http://localhost:8006/provision \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "agent-001",
    "manifest_path": "default.yaml",
    "access_tier": "standard"
  }'
```

Response:
```json
{
  "job_id": "uuid...",
  "status": "running",
  "message": "Provisioning started. Poll GET /provision/status/{job_id} for progress."
}
```

### Check Status

```bash
curl http://localhost:8006/provision/status/{job_id}
```

Response:
```json
{
  "job_id": "uuid...",
  "status": "running",
  "agent_id": "agent-001",
  "current_phase": "account_provisioning",
  "current_tool": "postgresql",
  "progress": 45,
  "tools_completed": 1,
  "tools_total": 3,
  "completed_phases": ["setup", "credential_generation"]
}
```

## Access Tiers

| Tier | Description |
|------|-------------|
| `minimal` | Read-only access to tools |
| `standard` | Read/write access (default) |
| `elevated` | Administrative access to own resources |
| `full` | Full administrative access (audited) |

## Tool Manifests

Manifests define which tools to provision. Located in `manifests/`:

- `default.yaml` - Full dev environment (PostgreSQL, Redis, Git)
- `minimal.yaml` - Lightweight (Git only)
- `full.yaml` - Complete environment with all features

### Manifest Format

```yaml
version: "1.0"
base_image: "python:3.11-slim"

environment:
  PYTHONUNBUFFERED: "1"

tools:
  - name: postgresql
    provisioner: postgres_provisioner
    access_level: read_write
    config:
      database_prefix: "agent_"
    onboarding:
      description: "PostgreSQL database"
      env_var: "POSTGRES_URL"
      getting_started: "Connect using: psql $POSTGRES_URL"
```

## Tool Provisioners

| Provisioner | Tool | Capabilities |
|-------------|------|--------------|
| `docker_provisioner` | Docker | Container lifecycle |
| `postgres_provisioner` | PostgreSQL | Database + user creation |
| `redis_provisioner` | Redis | ACL with key prefix |
| `git_provisioner` | Git | SSH keys + repo init |
| `generic_provisioner` | Custom | Template for extensions |

## Directory Structure

```
agent_provisioning_team/
├── models.py              # Domain models
├── orchestrator.py        # Phase controller
├── phases/                # Phase implementations
├── tool_agents/           # Tool provisioners
├── shared/                # Stores and utilities
├── api/                   # FastAPI endpoints
└── manifests/             # Tool manifest examples
```

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Start the API server
uvicorn agent_provisioning_team.api.main:app --host 0.0.0.0 --port 8006
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PROVISION_CREDENTIAL_KEY` | Fernet encryption key | Auto-generated |
| `POSTGRES_HOST` | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | PostgreSQL port | `5432` |
| `POSTGRES_USER` | PostgreSQL admin user | `postgres` |
| `POSTGRES_PASSWORD` | PostgreSQL admin password | - |
| `REDIS_HOST` | Redis host | `localhost` |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_PASSWORD` | Redis admin password | - |
