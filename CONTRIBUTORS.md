# Contributing to Khala

Thank you for your interest in contributing to Khala. This document covers everything you need to effectively contribute: setup, code standards, workflows, and pull request guidelines.

---

## Table of Contents

- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Code Standards](#code-standards)
- [Project Conventions](#project-conventions)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Areas to Contribute](#areas-to-contribute)
- [Communication](#communication)

---

## Getting Started

### Prerequisites

Before contributing, ensure you have:

| Tool | Version | Purpose |
|------|---------|---------|
| **Python** | 3.10+ | Agent teams |
| **Node.js** | 22.12+ or 20.19+ | UI and Software Engineering frontend builds |
| **npm** | 10+ | UI dependencies |
| **Git** | 2.x | Version control |
| **Ollama** | (optional) | Local LLM for agent testing |

We recommend [NVM](https://github.com/nvm-sh/nvm) for Node.js. The UI uses `.nvmrc` (Node 22.12).

### Fork and Clone

```bash
# Fork the repository on GitHub, then:
git clone https://github.com/YOUR_USERNAME/khala.git
cd khala
git remote add upstream https://github.com/ORIGINAL_OWNER/khala.git
```

### Branch Naming

Use descriptive branch names with a prefix:

- `feat/` – New feature (e.g. `feat/add-investment-team-ui`)
- `fix/` – Bug fix (e.g. `fix/soc2-polling-timeout`)
- `docs/` – Documentation only (e.g. `docs/update-api-mapping`)
- `refactor/` – Code refactor (e.g. `refactor/consolidate-llm-clients`)
- `test/` – Test additions or fixes (e.g. `test/blogging-research-agent`)

---

## Development Setup

### 1. Python Environment

```bash
# Create and activate a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# or: .venv\Scripts\activate   # Windows

# Install dependencies from repo root
pip install -r agents/requirements.txt
pip install -r agents/software_engineering_team/requirements.txt
pip install -r agents/blogging/requirements.txt

# For development (pytest, etc.)
pip install -e agents/software_engineering_team
pip install -e agents/blogging
```

### 2. Node.js / UI

```bash
cd user-interface
nvm use          # or: nvm install (if Node 22.12 not installed)
npm ci
```

Node 22.12 is recommended (see `.nvmrc`). Angular 19 requires Node >=18.19.1.

### 3. Environment Variables

Create a `.env` file in `agents/` (or export in your shell) for local development:

```bash
# Blogging (research agent uses Ollama web_search; same key as LLM)
OLLAMA_API_KEY=your_ollama_key

# Software Engineering (for real LLM runs)
LLM_PROVIDER=ollama
LLM_MODEL=qwen3.5:397b-cloud
LLM_BASE_URL=http://127.0.0.1:11434

# SOC2 (optional)
SOC2_LLM_PROVIDER=ollama
SOC2_LLM_MODEL=llama3.1
```

### 4. Verify Setup

```bash
# Run agent tests
pytest agents/software_engineering_team/tests/ -v
pytest agents/blogging/tests/ -v

# Run UI tests
cd user-interface && ng test --no-watch
```

---

## Code Standards

### Python (Agents)

- **Style:** Follow [PEP 8](https://pep8.org/). Use `black` or `ruff` for formatting if your editor supports it.
- **Type hints:** Use type annotations for function signatures and complex logic.
- **Docstrings:** Document public functions, classes, and modules. Prefer Google or NumPy style.
- **Imports:** Group imports: standard library, third-party, local. Use `isort`-compatible ordering.

Example:

```python
"""Module docstring."""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel


def process_spec(repo_path: Path, use_cache: bool = True) -> Optional[dict]:
    """Process initial_spec.md and return parsed structure.

    Args:
        repo_path: Path to git repository root.
        use_cache: Whether to use cached planning result.

    Returns:
        Parsed spec dict or None if invalid.
    """
    ...
```

### Software Engineering Team Conventions

The Software Engineering team enforces these rules in generated code; contributors should align with them:

- **Design by Contract:** Preconditions, postconditions, invariants on public APIs
- **SOLID:** Single responsibility, Open/Closed, Liskov, Interface segregation, Dependency inversion
- **Documentation:** Comment blocks on classes/methods: purpose, usage, constraints
- **Test coverage:** Minimum 85% where applicable
- **Git:** Work on `development` branch; Conventional Commits for messages

### TypeScript / Angular (UI)

- **Style:** Follow Angular style guide and project `tsconfig`/lint rules.
- **Components:** Prefer standalone components. Use `@Input()` / `@Output()` for data flow.
- **Services:** One service per API. Use `HttpClient` with typed interfaces.
- **Accessibility:** Use `[attr.aria-*]`, `aria-label`, `aria-live` where appropriate. See [user-interface/docs/ACCESSIBILITY.md](user-interface/docs/ACCESSIBILITY.md).

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): description

[optional body]

[optional footer]
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`.

Examples:

```
feat(blogging): add arxiv search to research agent
fix(ui): correct polling interval for job status
docs(readme): add docker port mapping table
```

---

## Project Conventions

### Monorepo Layout

- **`agents/`** – All Python agent teams. Each team is a package (e.g. `software_engineering_team`, `blogging`).
- **`user-interface/`** – Angular app. Feature components under `src/app/components/`, services under `src/app/services/`.
- **Shared code:** Prefer clear boundaries. Avoid cross-team imports unless there is a shared module.

### API Design

- Use FastAPI for agent HTTP APIs.
- Use Pydantic models for request/response validation.
- Expose `GET /health` for every API.
- Use consistent error response format (e.g. `{"detail": "..."}`).

### Adding a New Agent Team

1. Create `agents/<team_name>/` with `__init__.py`, `api/main.py`, models, and agents.
2. Add `requirements.txt` if the team has unique dependencies.
3. Add a supervisord program in `agents/supervisord.conf` if running in Docker.
4. Update `agents/Dockerfile` to copy the new team and install its requirements.
5. Add a dashboard and service in `user-interface/` if the team has an HTTP API.
6. Document the team in `agents/README.md` and the root `README.md`.

### Adding a New UI Dashboard

1. Create components under `user-interface/src/app/components/<feature>/`.
2. Add a service in `user-interface/src/app/services/` that calls the API.
3. Add route in `app.routes.ts`.
4. Add nav link in `app-shell.component`.
5. Update `user-interface/docs/API_MAPPING.md`.

---

## Testing

### Running Tests

```bash
# All Python tests
pytest

# Specific team
pytest agents/software_engineering_team/tests/ -v
pytest agents/blogging/tests/ -v

# With coverage
pytest agents/software_engineering_team/tests/ --cov=software_engineering_team --cov-report=html

# UI tests
cd user-interface && ng test --no-watch --code-coverage
```

### Writing Tests

- **Python:** Use pytest. Place tests in `tests/` next to the package or in `*/tests/`.
- **Angular:** Use Jasmine/Karma. Co-locate `.spec.ts` files with components/services.
- **Mocking:** Mock external APIs (Ollama web search, LLM) in unit tests. Use `DummyLLMClient` or `SOC2_LLM_PROVIDER=dummy` where supported.

### Test Requirements

- New features should include tests.
- Bug fixes should include a regression test when feasible.
- Ensure `pytest` and `ng test` pass before submitting a PR.

---

## Pull Request Process

### Before Submitting

1. **Sync with upstream:**
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run tests:**
   ```bash
   pytest
   cd user-interface && ng test --no-watch
   ```

3. **Check formatting:** Run any project linters (e.g. `ruff`, `prettier`) if configured.

### PR Checklist

- [ ] Branch is up to date with `main`
- [ ] Tests pass locally
- [ ] Code follows project conventions
- [ ] Documentation updated if needed (README, API_MAPPING, etc.)
- [ ] Commit messages follow Conventional Commits

### PR Description

Include:

- **Summary:** What changes and why
- **Type:** Bug fix, feature, refactor, docs
- **Testing:** How you verified the change
- **Screenshots/Logs:** For UI or behavior changes, if helpful

### Review

- Address review feedback promptly.
- Keep PRs focused. Prefer smaller, incremental changes over large monoliths.

---

## Areas to Contribute

### High-Value Areas

- **New agent capabilities:** Extend existing agents (e.g. new planning agents, tools).
- **UI improvements:** Accessibility, error handling, loading states, new dashboards.
- **Documentation:** READMEs, API docs, architecture diagrams, runbooks.
- **Tests:** Increase coverage, add integration tests, fix flaky tests.
- **Performance:** Caching, parallelization, context truncation in agents.

### Good First Issues

- Fix typos or clarify documentation
- Add missing `aria-label` or keyboard support in UI
- Add unit tests for uncovered modules
- Improve error messages or logging

### Out of Scope (Unless Discussed)

- Breaking API changes without migration path
- New agent teams that duplicate existing functionality
- Large refactors without prior discussion

---

## Communication

- **Issues:** Use GitHub Issues for bugs, feature requests, and questions.
- **Discussions:** Use GitHub Discussions for design ideas, architecture questions, or general chat.
- **Security:** Report security vulnerabilities privately (e.g. via maintainer contact or security policy) rather than in public issues.

---

Thank you for contributing to Khala.
