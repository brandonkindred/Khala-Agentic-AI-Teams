# Software Engineering Team

A multi-agent system that simulates a real software engineering team with a mix of seniority and domain expertise.

## Team Structure

| Agent | Role | Expertise |
|-------|------|------------|
| **Tech Lead** | Staff-level orchestrator | Uses initial_spec to generate full build plan; distributes work by dependency; tracks progress; triggers documentation |
| **Architecture Expert** | System designer | Designs system architecture from requirements; output used by all other agents |
| **DevOps Expert** | Infrastructure specialist | CI/CD pipelines, IaC (Terraform, etc.), Docker, networking |
| **Cybersecurity Expert** | Security specialist | Reviews code for security flaws per task (backend and frontend); remediates vulnerabilities |
| **Backend Expert** | Backend engineer | Implements solutions in Python or Java; runs autonomous workflow with quality gates |
| **Frontend Expert** | Frontend engineer | Implements solutions in Angular; runs unified workflow mirroring backend |
| **QA Expert** | Quality assurance | Reviews for bugs; produces integration/unit tests and README content (persisted to repo) |
| **Code Review Agent** | Code reviewer | Reviews code against spec, standards, and acceptance criteria |
| **Acceptance Verifier** | Criteria checker | Verifies each task acceptance criterion is satisfied with evidence |
| **Integration Agent** | Full-stack validator | Validates backend-frontend API contract alignment after workers complete |
| **Accessibility Expert** | A11y specialist | Reviews frontend for WCAG 2.2 compliance |
| **DbC Comments Agent** | Design by Contract | Adds pre/postconditions and invariants to code |
| **Documentation Agent** | Technical writer | Updates README and project docs |

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

1. **Load spec** – Read `initial_spec.md` from the repo.
2. **Architecture Expert** designs the system from product requirements.
3. **Tech Lead** generates a complete build plan and assigns tasks (git_setup, devops, backend, frontend).
4. **Backend and Frontend workers** run in parallel. Each task follows a unified workflow:
   - Create feature branch
   - Generate code (with clarification loop via Tech Lead if needed)
   - **Build verification** (pytest for backend, ng build for frontend)
   - **Code review** (against spec and standards)
   - **Acceptance criteria verification** (optional; per-criterion check)
   - **Security review** (per task for backend; per task for frontend)
   - **QA review** (backend: bugs + persisted integration/unit tests and README)
   - **Accessibility review** (frontend only)
   - **DbC comments** (add pre/postconditions)
   - Merge to development, Tech Lead review, Documentation update
5. **Integration phase** – After workers complete, Integration Agent validates backend-frontend API contract alignment.
6. **Final security** (full codebase) and **documentation** pass when Tech Lead requests.
7. **Retry path** – Failed tasks are retried through the same full workflow (build, code review, QA, a11y, security, DBC).

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
| `SW_LLM_MODEL` | Model name for Ollama | `qwen2.5-coder` |
| `SW_LLM_BASE_URL` | Ollama API base URL | `http://127.0.0.1:11434` |
| `SW_LLM_TIMEOUT` | Timeout in seconds | `1800` |
| `SW_LLM_MAX_RETRIES` | Max retries for 429/5xx errors | `4` |
| `SW_LLM_BACKOFF_BASE` | Base seconds for exponential backoff | `2` |
| `SW_LLM_BACKOFF_MAX_SECONDS` | Max backoff seconds | `60` |
| `SW_LLM_MAX_CONCURRENCY` | Max concurrent LLM calls | `2` |

Example with Ollama:
```bash
export SW_LLM_PROVIDER=ollama
export SW_LLM_MODEL=qwen2.5-coder
python -m agent_implementations.run_team
```

Ensure Ollama is running with the model (e.g. `ollama run qwen2.5-coder`).

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

## Pipeline Diagram

```
Spec → Architecture → Tech Lead Plan
         ↓
    [Backend Worker]     [Frontend Worker]
    (run_workflow)       (run_workflow)
    Build → CodeReview → AcceptanceVerifier → Security → QA → DBC → Merge
         ↓                      ↓
    Integration Agent (backend + frontend contract alignment)
         ↓
    Final Security + Documentation
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
├── integration_agent/   # Full-stack API contract validation
├── acceptance_verifier_agent/
├── code_review_agent/
├── dbc_comments_agent/
├── accessibility_agent/
├── documentation_agent/
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
