# shared_repo_context

Neutral, team-agnostic **repository scanning / context** utilities.

Reading a repository's source into an LLM-ready string, and the extension /
exclude-dir constants that keep every repo scanner consistent, are needed by both
the software-engineering team and the coding team. This package is the single home
for them so neither team imports the other's internals.

## Layout

| Module | Was | Responsibility |
|---|---|---|
| `repo_utils` | `software_engineering_team/shared/repo_utils.py` | `read_repo_code` scanner; `REPO_EXCLUDE_DIRS` / `REPO_INSPECT_EXCLUDE_DIRS` / `*_EXTENSIONS` constants; sensitive-path detection (`is_sensitive_path`, `is_secret_template_path`); `read_files_as_dict` / `read_repo_files_as_dict`; `truncate_for_context`. |

## Usage

```python
from shared_repo_context import read_repo_code, FULL_STACK_EXTENSIONS, REPO_INSPECT_EXCLUDE_DIRS

briefing = read_repo_code(repo_path, FULL_STACK_EXTENSIONS, exclude_dirs=REPO_INSPECT_EXCLUDE_DIRS)
```

## Contracts & conventions

- **Import-safe:** importing the package has no side effects. `truncate_for_context`
  lazily imports `llm_service` (a neutral module) only when passed an LLM handle.
- `read_repo_code` always excludes `.git` regardless of `exclude_dirs`.
- Depends on `backend/agents` being on `sys.path` (the `shared_*` convention).

## Budgeted scanner

`read_repo_code_budgeted(repo_path, *, extensions, exclude_dirs, max_chars, empty=...)`
is the single implementation behind the per-domain `_read_repo_code` readers in the
backend/frontend code-v2 teams and the ai-agent-development team. Those methods are
now thin delegators that pass their own extension/exclude sets and `max_chars`
budget — same `--- <relpath> ---` header, same stop-at-budget (whole files only)
behaviour as before, one implementation. coding_team's `_read_repo_context` keeps
its distinct 80-file / full-content contract but sources its filter constants
(`FULL_STACK_EXTENSIONS`, `REPO_INSPECT_EXCLUDE_DIRS`) from this package.
