# Strands Agents - Docker Deployment

This document describes how to run all 6 agent teams in a consistent, repeatable Docker environment with pre-installed tools (Node.js, Angular CLI, Git, Docker CLI).

## Prerequisites

- **Docker** (20.10+)
- **Docker Compose** (v2+)
- **Ollama** running on the host machine (for LLM inference)

Ensure Ollama is running and accessible before starting the container:
```bash
curl http://localhost:11434/api/tags
```

## Quick Start

```bash
# From project root
docker-compose up -d

# Verify all APIs are healthy
curl http://localhost:18000/health
curl http://localhost:18001/health
curl http://localhost:18002/health
curl http://localhost:18003/health
curl http://localhost:18004/health
curl http://localhost:18005/health
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SW_LLM_PROVIDER` | `ollama` | LLM provider (ollama or dummy) |
| `SW_LLM_BASE_URL` | `http://host.docker.internal:11434` | Ollama API URL. Use this to reach Ollama on the host. |
| `SW_LLM_MODEL` | `qwen3-coder-next:cloud` | Default model for agents |
| `SW_LLM_MODEL_<AGENT_KEY>` | - | Per-agent model override (e.g. `SW_LLM_MODEL_BACKEND_EXPERT`) |
| `PYTHONUNBUFFERED` | `1` | Ensures Python output is not buffered |

To override the default model, create a `.env` file with `SW_LLM_MODEL=your-model` and add it to the service, or run:
```bash
SW_LLM_MODEL=llama3.2:latest docker-compose up -d
```

## Port Mapping

| Port | Team | Key Endpoints |
|------|------|---------------|
| 18000 | 8000 | Software Engineering | `POST /run-team`, `GET /run-team/{id}`, `POST /clarification/sessions` |
| 18001 | 8001 | Blogging | `POST /research-and-review`, `POST /full-pipeline` |
| 18002 | 8002 | Market Research | `POST /market-research/run` |
| 18003 | 8003 | SOC2 Compliance | `POST /soc2-audit/run`, `GET /soc2-audit/status/{id}` |
| 18004 | 8004 | Social Marketing | `POST /social-marketing/run`, `GET /social-marketing/status/{id}` |
| 18005 | 8005 | Blog Research | `POST /research-and-review` |

### Example API Calls

```bash
# Software Engineering - start a run
curl -X POST http://localhost:18000/run-team \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/workspace/my-project"}'

# Blog Research
curl -X POST http://localhost:18005/research-and-review \
  -H "Content-Type: application/json" \
  -d '{"topic": "Docker best practices", "audience": {"skill_level": "intermediate"}}'
```

## Spec File

A spec file is copied into the image at build time as `/app/initial_spec.md`, so the Software Engineering Team API can run without mounting a spec from the host.

- **Default spec:** When no custom spec is provided, `docker/default_initial_spec.md` is used (Task Manager API example).
- **Custom spec at build time:** Pass `SPEC_FILE` as a build arg or env var. The path must be relative to the build context (project root).

```bash
# Build with custom spec (spec must be in build context)
docker build --build-arg SPEC_FILE=./my-project/initial_spec.md -t strands-agents .

# Or with docker-compose (SPEC_FILE is passed as build arg)
SPEC_FILE=./my-spec.md docker-compose build
```

**Using the baked-in spec:** When calling `/run-team`, use `repo_path: "/app"`:

```bash
curl -X POST http://localhost:18000/run-team \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "/app"}'
```

**Note:** The spec path in `SPEC_FILE` must be relative to the build context. To use a spec outside the project, copy it in first: `cp /path/to/spec.md ./my-spec.md` then `SPEC_FILE=./my-spec.md`.

## Volume Mounts

| Host Path | Container Path | Purpose |
|-----------|----------------|---------|
| `./workspace` | `/workspace` | Working directory for agent output (repos, specs, artifacts) |

**Docker-in-Docker:** The container runs its own Docker daemon (no host socket mount). The container must run with `privileged: true` for the daemon to function.

The Software Engineering team expects `repo_path` to point to a directory containing `initial_spec.md`. You can use the baked-in spec at `/app` (see above), or mount your project into `/workspace`:
```bash
docker-compose run -v $(pwd)/my-project:/workspace/my-project strands-agents
```

Or use the default `./workspace` directory - create `workspace/my-project/initial_spec.md` before calling `/run-team` with `repo_path: "/workspace/my-project"`.

## Building a Custom Image

```bash
docker build -t strands-agents .
```

## Verification

After building and starting the container, verify the setup:

```bash
# 1. Build completes without errors
docker build -t strands-agents .

# 2. Container starts
docker-compose up -d

# 3. Health endpoints respond
for port in 18000 18001 18002 18003 18004 18005; do
  echo -n "Port $port: "
  curl -s -o /dev/null -w "%{http_code}" "http://localhost:$port/health"
  echo
done

# 4. Tools available inside container
docker exec strands-agents node --version
docker exec strands-agents ng version
docker exec strands-agents git --version
docker exec strands-agents docker --version

# 5. All supervisor processes running
docker exec strands-agents supervisorctl status
```

## Troubleshooting

### Ollama Connection Issues

**Symptom:** Agents fail with "connection refused" or timeout errors.

**Solutions:**
1. Ensure Ollama is running on the host: `curl http://localhost:11434/api/tags`
2. On Linux, `host.docker.internal` requires `extra_hosts: host.docker.internal:host-gateway` (included in docker-compose.yml)
3. If using a custom Ollama URL, set `SW_LLM_BASE_URL` (e.g. `http://192.168.1.100:11434`)

### Docker Build Failures

**Symptom:** DevOps agent cannot run `docker build` - daemon not running or permission errors.

**Solutions:**
1. Ensure the container runs with `privileged: true` (required for Docker-in-Docker)
2. Check dockerd is running: `docker exec strands-agents supervisorctl status dockerd`
3. Verify docker works: `docker exec strands-agents docker info`

### Port Already in Use

**Symptom:** `failed to bind host port for 0.0.0.0:8000: address already in use`

**Solutions:**
1. Stop any existing strands-agents container: `docker-compose down`
2. Stop other services using ports 18000-18005 (or 8000-8005 if you changed the mapping)
3. The default host ports are 18000-18005 to avoid conflicts with Ollama (11434) and common dev servers

### Health Check Failures

**Symptom:** Container reports unhealthy or restarts repeatedly.

**Solutions:**
1. Check logs: `docker logs strands-agents`
2. Verify all 6 API processes started: `docker exec strands-agents supervisorctl status`
3. Ensure ports 8000-8005 are not in use on the host

### Tool Verification

Verify tools inside the container:
```bash
docker exec strands-agents node --version
docker exec strands-agents ng version
docker exec strands-agents git --version
docker exec strands-agents docker --version
```

## Security Considerations

**Docker-in-Docker (privileged):** The container runs with `privileged: true` to support an internal Docker daemon. This grants elevated capabilities. The container's Docker is isolated from the host—agents build images inside the container only.

**Recommendations:**
- Run on a dedicated CI/build host when possible
- Do not expose the container to untrusted networks
- The workspace volume is the only host path mounted; ensure it contains only trusted content
