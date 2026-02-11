# Software Engineering Team

A multi-agent system that simulates a real software engineering team with a mix of seniority and domain expertise.

## Team Structure

| Agent | Role | Expertise |
|-------|------|------------|
| **Tech Lead** | Staff-level engineer | Bridges product management and engineering; plans and orchestrates task distribution |
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
2. **Architecture Expert** designs the system from product requirements.
3. **Tech Lead** breaks down work and assigns tasks to specialists.
4. Specialists execute tasks in dependency order; each uses the architecture when implementing or validating.
5. **Security** and **QA** agents review code produced by Backend and Frontend.

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

By default, the script uses `DummyLLMClient` for testing without an LLM. To use a real model (e.g. Ollama):

1. Edit `run_team.py` and set `USE_DUMMY = False`.
2. Ensure Ollama is running with a model (e.g. `ollama run deepseek-r1`).

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
├── test_repo/        # Sample repo with initial_spec.md
├── pyproject.toml
└── requirements.txt
```

Each agent has:
- `agent.py` – Core logic
- `models.py` – Input/output Pydantic models
- `prompts.py` – LLM prompt templates
