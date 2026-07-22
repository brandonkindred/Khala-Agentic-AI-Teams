# shared.command_runner

Neutral, team-agnostic **command runner** and **build/test/lint error parser**.

Both the software-engineering team and the coding team run the same kinds of
subprocesses (frontend `npm`/`ng` build, `python -m pytest`, `ruff`/`eslint`
lint, project scaffolding) and need the same structured interpretation of the
output. This package is the single home for that logic so neither team has to
import the other's internals.

## Layout

| Module | Responsibility |
|---|---|
| `runner` | Core: subprocess primitives (`run_command`, `CommandResult`), frontend framework detection, `run_pytest`, `run_python_syntax_check`, `run_linter`. Depends on nothing else in this package at module load time. |
| `nvm` | NVM (Node Version Manager) install/detection and `run_command_with_nvm`, which runs a command under a managed Node version. |
| `angular_repair` | Best-effort fixes applied to an Angular project before a build (`package.json` deps, `tsconfig.json`, `app.config.ts`, Material theme, `@@angular` typo, ReactiveFormsModule) and `ensure_frontend_dependencies_installed`. |
| `scaffolding` | Writes a minimal Angular/React/FastAPI project skeleton (`ensure_frontend_project_initialized`, `ensure_backend_project_initialized`). |
| `smoke_test` | Starts a frontend dev server (`npm start`/`dev` or `ng serve`) briefly to confirm it compiles and runs. |
| `error_parsing` | Turn raw tool output into `ParsedFailure` records (`parse_command_failure`), render `build_agent_feedback`, `normalize_error_signature` for loop detection. |

## Usage

```python
from shared.command_runner import run_pytest, CommandResult, parse_command_failure

result: CommandResult = run_pytest(project_path)
if not result.success:
    failures = result.parsed_failures("pytest")  # list[ParsedFailure]
```

## Contracts & conventions

- **Import-safe:** importing the package has no side effects and starts no
  threads. `runner` is stdlib-only at import time; it lazily imports
  `error_parsing` (in `CommandResult.parsed_failures`) and, inside
  `run_frontend_build`/`run_linter`, the sibling `angular_repair`/`nvm`
  modules it dispatches to — all in-package imports with no team
  dependency. `scaffolding.ensure_backend_project_initialized` lazily
  imports `shared.git.git_utils`.
- Tests that stub subprocess behaviour patch the module where the *symbol
  under test now lives*, not necessarily where it's defined — e.g.
  `@patch("shared.command_runner.runner.subprocess.run")` for `run_command`,
  `@patch("shared.command_runner.smoke_test.subprocess.Popen")` for
  `run_ng_serve_smoke_test`, `@patch("shared.command_runner.angular_repair.run_command")`
  for `ensure_frontend_dependencies_installed`.
- Depends on `backend/` being on `sys.path` (the repo-wide `shared.*`
  convention).
