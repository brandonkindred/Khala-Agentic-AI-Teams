# shared_command_runner

Neutral, team-agnostic **command runner** and **build/test/lint error parser**.

Both the software-engineering team and the coding team run the same kinds of
subprocesses (frontend `npm`/`ng` build, `python -m pytest`, `ruff`/`eslint`
lint, project scaffolding) and need the same structured interpretation of the
output. This package is the single home for that logic so neither team has to
import the other's internals.

## Layout

| Module | Was | Responsibility |
|---|---|---|
| `runner` | `software_engineering_team/shared/command_runner.py` | Subprocess primitives (`run_command`, `run_command_with_nvm`), frontend build/serve, `run_pytest`, `run_linter`, project scaffolding, `CommandResult`. |
| `error_parsing` | `software_engineering_team/shared/error_parsing.py` | Turn raw tool output into `ParsedFailure` records (`parse_command_failure`), render `build_agent_feedback`, `normalize_error_signature` for loop detection. |

## Usage

```python
from shared_command_runner import run_pytest, CommandResult, parse_command_failure

result: CommandResult = run_pytest(project_path)
if not result.success:
    failures = result.parsed_failures("pytest")  # list[ParsedFailure]
```

## Contracts & conventions

- **Import-safe:** importing the package has no side effects and starts no
  threads. `runner` is stdlib-only at import time and lazily imports
  `error_parsing` (in `CommandResult.parsed_failures`) — an in-package import
  with no team dependency.
- Tests that stub subprocess behaviour patch the module where the symbol is
  used, e.g. `@patch("shared_command_runner.runner.subprocess.run")` or
  `@patch("shared_command_runner.runner.run_command")`.
- Depends on `backend/agents` being on `sys.path` (the repo-wide `shared_*`
  convention).

## Note

`runner.ensure_backend_project_initialized` still lazily imports git helpers
from `software_engineering_team.shared.git_utils`; that integration-only path
moves to the neutral `shared_git` package when the git utilities are promoted.
