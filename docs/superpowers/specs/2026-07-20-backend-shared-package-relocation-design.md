# Backend shared package relocation

**Date:** 2026-07-20  
**Status:** Approved for implementation planning

## Goal

Move all top-level `backend/agents/shared_*` infrastructure packages into a single platform package at `backend/shared/`, outside the agents tree, and rename imports to drop the `shared_` prefix (`shared.postgres`, `shared.temporal`, …). Hard cutover — no compatibility shims.

## Motivation

The twenty `shared_*` directories sit as siblings of every agent team under `backend/agents/`, which blurs “platform infra” vs “team code.” They already resolve as top-level modules only because `backend/agents` is on `sys.path`. Relocating them under `backend/shared/` makes that boundary explicit and gives a cleaner public namespace.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Target location | `backend/shared/` (sibling of `agents/`, `unified_api/`, `job_service/`) |
| Import style | Nested package, drop prefix: `from shared.postgres import …` |
| Compatibility | Hard cutover — rewrite all call sites; no `shared_postgres` shims |
| Out of scope | `software_engineering_team/shared/` and other team-local `*/shared/` helpers |

## Target layout

```
backend/shared/
  __init__.py
  agent_invoke/
  app/
  command_runner/
  concurrency/
  dev_models/
  env/
  env_config/
  git/
  graph/
  hitl/
  http/
  job_event_bus/
  llm_recovery/
  neo4j/
  observability/
  postgres/
  repo_context/
  run_thread_registry/
  sse/
  temporal/
  infra.coveragerc          # moved from agents/shared_infra.coveragerc
```

### Package rename map

| Old top-level package | New import root |
|---|---|
| `shared_agent_invoke` | `shared.agent_invoke` |
| `shared_app` | `shared.app` |
| `shared_command_runner` | `shared.command_runner` |
| `shared_concurrency` | `shared.concurrency` |
| `shared_dev_models` | `shared.dev_models` |
| `shared_env` | `shared.env` |
| `shared_env_config` | `shared.env_config` |
| `shared_git` | `shared.git` |
| `shared_graph` | `shared.graph` |
| `shared_hitl` | `shared.hitl` |
| `shared_http` | `shared.http` |
| `shared_job_event_bus` | `shared.job_event_bus` |
| `shared_llm_recovery` | `shared.llm_recovery` |
| `shared_neo4j` | `shared.neo4j` |
| `shared_observability` | `shared.observability` |
| `shared_postgres` | `shared.postgres` |
| `shared_repo_context` | `shared.repo_context` |
| `shared_run_thread_registry` | `shared.run_thread_registry` |
| `shared_sse` | `shared.sse` |
| `shared_temporal` | `shared.temporal` |

Submodules keep their relative structure (e.g. `shared_postgres.testing` → `shared.postgres.testing`).

## Import resolution

`from shared.postgres import …` requires **`backend/`** on `sys.path` so Python finds the top-level `shared` package.

Today many entry points already insert `backend/` (`run_unified_api.py`, `unified_api/main.py`, parts of `conftest.py`). Gaps to fix:

- Entry points / CI jobs / scripts that only add `backend/agents` (e.g. job-service tests with `PYTHONPATH=../agents`, team scripts with `PYTHONPATH=agents`) must also include `backend/`.
- Recommended pattern from `backend/`: `PYTHONPATH=.:agents` (or absolute equivalents). From a subdirectory under `backend/`: `PYTHONPATH=..:../agents` as needed.
- Package-internal absolute imports rewrite from `shared_<name>.…` to `shared.<name>.…` (or relative imports within the package).
- Docstrings that say “depends on `backend/agents` on `sys.path` (the `shared_*` convention)” update to “depends on `backend/` on `sys.path`.”

No collision with `software_engineering_team.shared` — that remains a subpackage of the SE team module.

## Migration plan (mechanical)

1. Create `backend/shared/__init__.py`.
2. `git mv` each `backend/agents/shared_<name>/` → `backend/shared/<name>/`.
3. `git mv` `backend/agents/shared_infra.coveragerc` → `backend/shared/infra.coveragerc`.
4. Rewrite imports and string references across the repo (`shared_<name>` → `shared.<name>`, including registries, tests, coverage omit paths, and docs).
5. Fix path bootstrap / `PYTHONPATH` so every consumer that needs shared infra has `backend/` on `sys.path`.
6. Update CI path filters, pytest/`--cov` targets, and combine-shared-infra `--rcfile` paths.
7. Update orientation docs (`CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/ENV_VARS.md`, `backend/agents/README.md`, per-package READMEs).

No behavioral changes to the packages themselves beyond import/path wiring.

## CI, coverage, and docs

- **Path filters** in `.github/workflows/ci.yml`: replace `backend/agents/shared_*/**` with `backend/shared/<name>/**` (and related comments / change-detection keys).
- **Pytest**: e.g. run from `backend/` as `pytest shared/postgres/tests/ --cov=shared.postgres` (adjust per job).
- **Coverage config**: omit patterns such as `*/shared_git/git_utils.py` become `*/shared/git/git_utils.py`; combine job points at `shared/infra.coveragerc`.
- **Docs**: paths and import examples updated to the new layout. Do not introduce GitHub issue numbers in new source/docs text (repo rule); PR body may still use `Closes #N` when a PR is opened.

## Verification

- Smoke: `python -c "from shared.postgres import TeamSchema; from shared.temporal import is_temporal_enabled"` with `backend/` on `PYTHONPATH`.
- Run shared-package unit tests and the live Postgres shared-postgres job equivalent under the new paths.
- Confirm job-service / unified-api suites that import shared infra still resolve.
- Confirm no remaining `from shared_` / `import shared_` references for the relocated packages (except historical notes if intentionally retained — prefer updating those too).

## Non-goals

- Refactoring package internals or merging packages.
- Changing Temporal / Postgres registration semantics.
- Moving or renaming `software_engineering_team/shared/`.
- Introducing a pip-installable distribution for `shared` (path-based import remains the convention).

## Risks

- **Incomplete path bootstrap:** a script or CI job that only has `agents/` on `sys.path` will fail with `ModuleNotFoundError: shared`. Mitigate by grepping for `PYTHONPATH` / `sys.path` patterns and fixing known CI jobs (job-service) in the same change.
- **Large diff noise:** hundreds of import rewrites. Prefer mechanical search-replace with a verified map; keep behavior untouched so review stays focused on paths.
- **Name confusion in docs:** “shared” now means both `backend/shared/` (platform) and `software_engineering_team/shared/` (team). Docs should say “platform shared package” vs “SE team shared helpers” where ambiguity matters.
