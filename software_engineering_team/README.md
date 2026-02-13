# Software Engineering Team

A multi-agent system that simulates a real software engineering team with a mix of seniority and domain expertise.

## Team Structure

| Agent | Role | Expertise |
|-------|------|------------|
| **Tech Lead** | Staff-level orchestrator | Uses initial_spec to generate full build plan; distributes work by dependency (Security only after code exists); tracks progress |
| **Architecture Expert** | System designer | Designs system architecture from requirements; output used by all other agents |
| **DevOps Expert** | Infrastructure specialist | CI/CD pipelines, IaC (Terraform, etc.), Docker, networking |
| **Cybersecurity Expert** | Security specialist | Reviews code for security flaws; remediates vulnerabilities |
| **Backend Expert** | Backend engineer | Implements solutions in Python or Java |
| **Frontend Expert** | Frontend engineer | Implements solutions in Angular |
| **QA Expert** | Quality assurance | Reviews for bugs, fixes them, writes integration tests, performs live testing |

## Coding Standards

All agents enforce these rules for produced code:

| Rule | Description |
|------|--------------|
| **Design by Contract** | Preconditions, postconditions, and invariants on all public APIs |
| **SOLID** | Single responsibility, Open/Closed, Liskov, Interface segregation, Dependency inversion |
| **Documentation** | Comment blocks on every class/method/function: how used, why it exists, constraints enforced |
| **Test Coverage** | Minimum 85% coverage; CI fails if below |
| **README** | Must include build, run, test, and deploy instructions |
| **Git Branching** | Work on `development` branch; PR to merge into `main`. Tech Lead creates `development` if missing |
| **Commit Messages** | Conventional Commits format: `type(scope): description` (feat, fix, docs, test, ci, etc.) |

## Flow

1. **Git setup** – Ensure `development` branch exists (created from `main` if missing).
2. **Load spec** – Read `initial_spec.md` from the repo.
3. **Architecture Expert** designs the system from product requirements.
4. **Tech Lead** retrieves the spec, generates a complete build plan, and assigns tasks with dependencies:
   - DevOps (CI/CD, Docker) runs early
   - Backend and Frontend implement per spec
   - Security runs only after Backend/Frontend have produced code to review
   - QA runs only after Security has reviewed the code
5. Specialists execute in dependency order; outputs (code) are passed to Security and QA.

## Requirements

- **Frontend builds:** NVM and Node v22.12+ (or v20.19+). The pipeline uses NVM to run Angular CLI. Install [NVM](https://github.com/nvm-sh/nvm) and run `nvm install 22.12`.

## Quick Start

```bash
cd software_engineering_team
pip install -r requirements.txt
python -m agent_implementations.run_team
```

Or from the project root:

```bash
python software_engineering_team/agent_implementations/run_team.py
```

By default, the script uses `DummyLLMClient` for testing without an LLM. To use a real model (e.g. Ollama), set environment variables or edit `run_team.py` and set `USE_DUMMY = False`.

**LLM configuration (environment variables):**

| Variable | Description | Default |
|----------|-------------|---------|
| `SW_LLM_PROVIDER` | `dummy` or `ollama` | `dummy` |
| `SW_LLM_MODEL` | Model name for Ollama | `deepseek-r1` |
| `SW_LLM_BASE_URL` | Ollama API base URL | `http://127.0.0.1:11434` |
| `SW_LLM_TIMEOUT` | Timeout in seconds | `1800` |

Example with Ollama:
```bash
export SW_LLM_PROVIDER=ollama
export SW_LLM_MODEL=deepseek-r1
python -m agent_implementations.run_team
```

Ensure Ollama is running with the model (e.g. `ollama run deepseek-r1`).

## API

An HTTP API lets you run the team on a git repo by providing a local path:

```bash
# Start the API server
cd software_engineering_team
pip install -r requirements.txt
python agent_implementations/run_api_server.py
```

Then POST to `http://127.0.0.1:8000/run-team`:

```json
{
  "repo_path": "/path/to/your/git/repo",
  "use_llm_for_spec": true
}
```

**Requirements:**
- `repo_path` must be a valid directory and a git repository (has `.git`)
- The repo must contain `initial_spec.md` at the root with the full project specification

**Response:** Architecture overview, task IDs, task results, `git_branch_setup` (development branch), and status.

Use `test_repo/` as a sample (includes `initial_spec.md`):
```bash
curl -X POST http://127.0.0.1:8000/run-team \
  -H "Content-Type: application/json" \
  -d '{"repo_path": "./test_repo"}'
```

## Logging and debugging

Agents log progress at INFO level so you can see what’s happening at each step.

**When running the API server**, logs go to stderr. Example output:

```
15:57:33 | INFO    | spec_parser | Parsing spec with LLM (1234 chars)
15:57:33 | INFO    | architecture_agent.agent | Architecture Expert: starting design for Task Manager API
15:57:33 | INFO    | architecture_agent.agent | Architecture Expert: done, 2 components
15:57:33 | INFO    | tech_lead_agent.agent | Tech Lead: planning tasks for Task Manager API
15:57:33 | INFO    | tech_lead_agent.agent | Tech Lead: assigned 2 tasks in order ['t1', 't2']
15:57:33 | INFO    | api.main | Pipeline: Task t1 (backend) -> backend
15:57:33 | INFO    | backend_agent.agent | Backend: implementing task 'Implement API'
15:57:33 | INFO    | backend_agent.agent | Backend: done, code=0 chars, summary=0 chars
```

**Verbose mode (DEBUG):** For more detail, use `shared.logging_config`:

```python
from shared.logging_config import setup_logging
setup_logging(level=logging.INFO, verbose=True)  # Agent loggers at DEBUG
```

**Write logs to a file:**

```python
from pathlib import Path
from shared.logging_config import setup_logging
setup_logging(level=logging.INFO, log_file=Path("agent.log"))
```

**Run tests with visible logs:**

```bash
pytest tests/ -v --log-cli-level=INFO
```

## Project Layout

```
software_engineering_team/
├── api/
│   └── main.py       # FastAPI app with /run-team endpoint
├── spec_parser.py    # Parses initial_spec.md into ProductRequirements
├── shared/            # LLM client, models, coding_standards, git_utils
├── architecture_agent/
├── tech_lead_agent/
├── devops_agent/
├── security_agent/
├── backend_agent/
├── frontend_agent/
├── qa_agent/
├── agent_implementations/
│   ├── run_team.py   # CLI orchestration script
│   └── run_api_server.py
├── shared/
│   └── logging_config.py  # setup_logging() for consistent agent logs
├── tests/            # pytest suite (spec, agents, pipeline, API)
├── test_repo/        # Sample repo with initial_spec.md
├── pyproject.toml
└── requirements.txt
```

Each agent has:
- `agent.py` – Core logic
- `models.py` – Input/output Pydantic models
- `prompts.py` – LLM prompt templates
