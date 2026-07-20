# Backend Shared Package Relocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate all `backend/agents/shared_*` infrastructure packages to `backend/shared/<name>/` and hard-cutover imports to `shared.<name>` (no shims).

**Architecture:** Create a top-level `shared` package under `backend/` (sibling of `agents/`). Resolve imports via existing `backend/` on `sys.path` / `pytest.ini` `pythonpath = agents .`. Mechanically rewrite every `shared_*` import and update CI, Dockerfiles, coverage, and docs in the same change set.

**Tech Stack:** Python 3.10+, pytest, ruff, GitHub Actions, Docker multi-service images.

**Spec:** `docs/superpowers/specs/2026-07-20-backend-shared-package-relocation-design.md`

## Global Constraints

- Hard cutover only — no `shared_postgres` (etc.) compatibility shims.
- Do not move or rename `software_engineering_team/shared/` or other team-local `*/shared/` trees.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- No behavioral changes inside the packages beyond import/path wiring.
- Preserve ≥90% coverage gates for shared packages and teams.
- DbC: only update existing contract docstrings that mention the old path convention; do not invent new APIs.

## File map

| Path | Responsibility after move |
|---|---|
| `backend/shared/__init__.py` | Platform shared package root |
| `backend/shared/<name>/` | Former `backend/agents/shared_<name>/` trees (20 packages) |
| `backend/shared/infra.coveragerc` | Former `agents/shared_infra.coveragerc` |
| All `from shared_*` / `import shared_*` call sites | Rewrite to `shared.<name>` |
| `backend/pytest.ini` | Ensure `shared` is on `testpaths`; `pythonpath` already has `.` |
| `backend/conftest.py` | Keep `agents/` on path; `backend/` already is rootdir via pytest |
| `backend/Dockerfile` | `COPY shared /app/shared` |
| `backend/team_service/Dockerfile` | `COPY shared/ /app/shared/` (PYTHONPATH already includes `/app`) |
| `backend/blogging_service/Dockerfile` | Replace per-package `COPY agents/shared_*` with `COPY shared/ /app/shared/` |
| `.github/workflows/ci.yml` | Path filters, pytest/`--cov`, PYTHONPATH comments, combine includes |
| `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/ENV_VARS.md`, package READMEs | Path/import examples |

### Rename map (rewrite longest names first)

| Old | New |
|---|---|
| `shared_run_thread_registry` | `shared.run_thread_registry` |
| `shared_command_runner` | `shared.command_runner` |
| `shared_job_event_bus` | `shared.job_event_bus` |
| `shared_repo_context` | `shared.repo_context` |
| `shared_llm_recovery` | `shared.llm_recovery` |
| `shared_agent_invoke` | `shared.agent_invoke` |
| `shared_observability` | `shared.observability` |
| `shared_concurrency` | `shared.concurrency` |
| `shared_env_config` | `shared.env_config` |
| `shared_dev_models` | `shared.dev_models` |
| `shared_postgres` | `shared.postgres` |
| `shared_temporal` | `shared.temporal` |
| `shared_neo4j` | `shared.neo4j` |
| `shared_graph` | `shared.graph` |
| `shared_hitl` | `shared.hitl` |
| `shared_http` | `shared.http` |
| `shared_sse` | `shared.sse` |
| `shared_app` | `shared.app` |
| `shared_git` | `shared.git` |
| `shared_env` | `shared.env` |

Filesystem: `backend/agents/shared_<suffix>/` → `backend/shared/<suffix>/`.

---

### Task 1: Create `backend/shared` and git-mv packages

**Files:**
- Create: `backend/shared/__init__.py`
- Move: all 20 `backend/agents/shared_*/` directories → `backend/shared/<suffix>/`
- Move: `backend/agents/shared_infra.coveragerc` → `backend/shared/infra.coveragerc`

**Interfaces:**
- Produces: importable package root `shared` when `backend/` is on `sys.path`
- Produces: `shared.postgres`, `shared.temporal`, … as subpackages (still containing old absolute imports until Task 2)

- [ ] **Step 1: Create package root**

```bash
mkdir -p backend/shared
printf '%s\n' \
  '"""Platform-shared infrastructure packages for Khala backend services.' \
  '' \
  'Subpackages live as ``shared.<name>`` (e.g. ``shared.postgres``).' \
  'Requires ``backend/`` on ``sys.path``.' \
  '"""' \
  > backend/shared/__init__.py
```

- [ ] **Step 2: git-mv each package (run from repo root)**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams

for old in \
  shared_agent_invoke shared_app shared_command_runner shared_concurrency \
  shared_dev_models shared_env shared_env_config shared_git shared_graph \
  shared_hitl shared_http shared_job_event_bus shared_llm_recovery \
  shared_neo4j shared_observability shared_postgres shared_repo_context \
  shared_run_thread_registry shared_sse shared_temporal
do
  suffix="${old#shared_}"
  git mv "backend/agents/${old}" "backend/shared/${suffix}"
done

git mv backend/agents/shared_infra.coveragerc backend/shared/infra.coveragerc
```

- [ ] **Step 3: Confirm layout**

```bash
ls backend/shared
# Expected: __init__.py, agent_invoke, app, command_runner, …, temporal, infra.coveragerc
test ! -e backend/agents/shared_postgres
test ! -e backend/agents/shared_infra.coveragerc
```

- [ ] **Step 4: Commit**

```bash
git add backend/shared
git commit -m "$(cat <<'EOF'
Move shared_* packages under backend/shared/.

EOF
)"
```

---

### Task 2: Mechanical import and path-string rewrite

**Files:**
- Modify: every file under the repo that references `shared_<name>` as a Python module or filesystem path for these packages (code, tests, CI, Dockerfiles, docs). Prefer a one-shot script; then spot-fix leftovers.

**Interfaces:**
- Consumes: rename map above (longest-first)
- Produces: all platform imports as `shared.<name>` / `shared.<name>.submodule`

- [ ] **Step 1: Run rewrite script from repo root**

Save and run (do **not** touch `software_engineering_team/shared/` via mistaken replacements of bare `shared/` paths that are team-local — only replace the listed `shared_*` tokens and `agents/shared_*` path prefixes):

```bash
python3 <<'PY'
from pathlib import Path

ROOT = Path(".")
SKIP_DIRS = {".git", "venv", ".venv", "node_modules", "__pycache__", ".hypothesis", "dist"}
# Longest-first so shared_env_config is not partially rewritten by shared_env.
PAIRS = [
    ("shared_run_thread_registry", "shared.run_thread_registry"),
    ("shared_command_runner", "shared.command_runner"),
    ("shared_job_event_bus", "shared.job_event_bus"),
    ("shared_repo_context", "shared.repo_context"),
    ("shared_llm_recovery", "shared.llm_recovery"),
    ("shared_agent_invoke", "shared.agent_invoke"),
    ("shared_observability", "shared.observability"),
    ("shared_concurrency", "shared.concurrency"),
    ("shared_env_config", "shared.env_config"),
    ("shared_dev_models", "shared.dev_models"),
    ("shared_postgres", "shared.postgres"),
    ("shared_temporal", "shared.temporal"),
    ("shared_neo4j", "shared.neo4j"),
    ("shared_graph", "shared.graph"),
    ("shared_hitl", "shared.hitl"),
    ("shared_http", "shared.http"),
    ("shared_sse", "shared.sse"),
    ("shared_app", "shared.app"),
    ("shared_git", "shared.git"),
    ("shared_env", "shared.env"),
]
PATH_PAIRS = [
    ("backend/agents/shared_" + old.removeprefix("shared_"), "backend/shared/" + old.removeprefix("shared_"))
    for old, _ in PAIRS
] + [
    ("agents/shared_" + old.removeprefix("shared_"), "shared/" + old.removeprefix("shared_"))
    for old, _ in PAIRS
] + [
    ("backend/agents/shared_infra.coveragerc", "backend/shared/infra.coveragerc"),
    ("agents/shared_infra.coveragerc", "shared/infra.coveragerc"),
    ("shared_infra.coveragerc", "shared/infra.coveragerc"),
]

TEXT_EXTS = {
    ".py", ".md", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".coveragerc",
    ".txt", ".sh", "Dockerfile",
}

SKIP_FILES = {
    "docs/superpowers/specs/2026-07-20-backend-shared-package-relocation-design.md",
    "docs/superpowers/plans/2026-07-20-backend-shared-package-relocation.md",
}

def should_skip(path: Path) -> bool:
    if any(p in SKIP_DIRS for p in path.parts):
        return True
    return path.as_posix() in SKIP_FILES

changed = []
for path in ROOT.rglob("*"):
    if not path.is_file() or should_skip(path):
        continue
    if path.suffix not in TEXT_EXTS and path.name not in TEXT_EXTS and not path.name.startswith("Dockerfile"):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    orig = text
    for old_path, new_path in PATH_PAIRS:
        text = text.replace(old_path, new_path)
    for old, new in PAIRS:
        text = text.replace(old, new)
    if text != orig:
        path.write_text(text, encoding="utf-8")
        changed.append(str(path))
print(f"updated {len(changed)} files")
for p in changed[:50]:
    print(p)
if len(changed) > 50:
    print(f"... and {len(changed) - 50} more")
PY
```

- [ ] **Step 2: Fix coveragerc omit + combine includes manually if the script left hybrid paths**

In `backend/shared/infra.coveragerc`, ensure:

```ini
omit =
    */tests/*
    */__pycache__/*
    */shared/git/git_utils.py
```

In `.github/workflows/ci.yml` `combine-shared-infra` report step, `--include` must use path globs like:

```text
--include='*/shared/command_runner/*,*/shared/repo_context/*,*/shared/llm_recovery/*,*/shared/dev_models/*,*/shared/git/*,*/shared/hitl/*,*/shared/run_thread_registry/*'
```

And SE `emit_shared_cov` `--cov=` flags must be:

```text
--cov=shared.command_runner
--cov=shared.repo_context
--cov=shared.llm_recovery
--cov=shared.dev_models
--cov=shared.git
--cov=shared.hitl
--cov=shared.run_thread_registry
```

- [ ] **Step 3: Grep for leftovers (must be empty of live imports)**

```bash
rg -n 'from shared_|import shared_|shared_postgres|shared_temporal|agents/shared_' \
  --glob '!docs/superpowers/specs/2026-07-20-backend-shared-package-relocation-design.md' \
  --glob '!docs/superpowers/plans/2026-07-20-backend-shared-package-relocation.md' \
  backend .github CLAUDE.md docs || true
```

Expected: no remaining live module imports of old names. The design/plan files are skipped by the script so their Old→New tables stay intact.

- [ ] **Step 4: Smoke import (after Task 1+2; before Docker/CI polish is OK)**

```bash
cd backend && PYTHONPATH=.:agents python -c \
  "from shared.postgres import TeamSchema; from shared.temporal import is_temporal_enabled; print('ok', TeamSchema, is_temporal_enabled)"
```

Expected: prints `ok …` without ImportError. If package-internal imports still fail, the rewrite missed a file — fix and re-run.

- [ ] **Step 5: Commit**

```bash
git add -A
git status   # review; do NOT stage unrelated .env / .kiro / design HTML
git commit -m "$(cat <<'EOF'
Rewrite shared_* imports to shared.<name>.

EOF
)"
```

---

### Task 3: Bootstrap paths, pytest, and Docker images

**Files:**
- Modify: `backend/pytest.ini` (`testpaths`)
- Modify: `backend/Dockerfile`
- Modify: `backend/team_service/Dockerfile`
- Modify: `backend/blogging_service/Dockerfile`
- Modify: `backend/conftest.py` only if a comment still claims shared packages live under `agents/`
- Modify: any team scripts whose docstring says `PYTHONPATH=agents` alone when they import `shared.*`

**Interfaces:**
- Consumes: `shared` package at `backend/shared/`
- Produces: images and local pytest that resolve `shared.*` and team packages

- [ ] **Step 1: Update `backend/pytest.ini`**

Set:

```ini
pythonpath = agents .
testpaths = agents shared unified_api agent_sandbox_runtime team_service
```

Keep existing comments; add one line noting platform shared tests live under `shared/`.

- [ ] **Step 2: Update `backend/Dockerfile`**

After `COPY agents /app/agents`, add:

```dockerfile
COPY shared /app/shared
```

`PYTHONPATH=/app:/app/agents` already finds `shared`.

- [ ] **Step 3: Update `backend/team_service/Dockerfile`**

After `COPY agents/ /app/agents/`, add:

```dockerfile
COPY shared/ /app/shared/
```

`PYTHONPATH` already includes `/app`.

- [ ] **Step 4: Update `backend/blogging_service/Dockerfile`**

Replace the many `COPY agents/shared_* /app/shared_*` blocks with a single tree copy (keep llm_service / integrations / blogging copies as-is):

```dockerfile
# Platform shared packages (shared.env, shared.postgres, shared.temporal, …).
COPY shared/ /app/shared/
```

Remove obsolete per-package COPY lines for: `shared_env`, `shared_env_config`, `shared_job_event_bus`, `shared_sse`, `shared_llm_recovery`, `shared_concurrency`, `shared_http`, `shared_postgres`, `shared_temporal`, `shared_observability`, `shared_app`.

Update comments that named old modules (e.g. `shared_postgres.client` → `shared.postgres.client`).

`ENV PYTHONPATH=/app` remains correct (`shared` is under `/app/shared`).

- [ ] **Step 5: Fix script/doc PYTHONPATH hints that only list `agents`**

For investment_team scripts (and similar) that say `PYTHONPATH=agents`, change to `PYTHONPATH=.:agents` when run from `backend/`, or document both roots. Grep:

```bash
rg -n 'PYTHONPATH=agents' backend --glob '*.py' --glob '*.md'
```

- [ ] **Step 6: Commit**

```bash
git add backend/pytest.ini backend/Dockerfile backend/team_service/Dockerfile \
  backend/blogging_service/Dockerfile backend/conftest.py
# plus any script docstring fixes
git commit -m "$(cat <<'EOF'
Wire Docker and pytest to backend/shared.

EOF
)"
```

---

### Task 4: CI workflow path filters and job commands

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: packages under `backend/shared/<name>/`
- Produces: change detection + test jobs that exercise `shared.*`

- [ ] **Step 1: Update every paths-filter entry**

Replace each `backend/agents/shared_<suffix>/**` with `backend/shared/<suffix>/**`. Also add `backend/shared/**` to `shared_backend` if not already covered by the individual entries.

Include all packages that belong in `shared_backend` (at least the ones currently listed, plus any of the twenty that were previously omitted if they should fan out all teams — match prior intent; do not silently drop filters).

- [ ] **Step 2: Update `test-shared-postgres`**

Preferred working directory `backend` (clearer):

```yaml
defaults:
  run:
    working-directory: backend
...
- name: register_all_team_schemas against live Postgres
  env:
    PYTHONPATH: .:agents
  run: python -c "from shared.postgres import register_all_team_schemas; r = register_all_team_schemas(); assert all(r.values()), r"
- run: >-
    pytest shared/postgres/tests/ -v --tb=short -n 4
    --cov=shared.postgres
    --cov-report=term-missing
    --cov-fail-under=90
```

If keeping `working-directory: backend/agents`, use `PYTHONPATH: ..` and `pytest ../shared/postgres/tests/ --cov=shared.postgres` instead — pick one style and stay consistent with neo4j.

- [ ] **Step 3: Update `test-shared-neo4j` similarly**

```text
pytest shared/neo4j/tests/ … --cov=shared.neo4j
```

- [ ] **Step 4: Update `combine-shared-infra`**

- `--rcfile=shared/infra.coveragerc` (if cwd is `backend`) **or** `--rcfile=../shared/infra.coveragerc` (if cwd stays `backend/agents`)
- Update `--include` globs to `*/shared/command_runner/*` etc. (filesystem paths, not dotted module names)
- Update comments referencing `shared_infra.coveragerc` / `shared_git`

Coverage artifact download path: if SE still writes `.coverage.*` into `backend/agents/`, leave download `path: backend/agents/` and run `coverage combine` from that cwd; only the rcfile/include paths must point at the new tree.

- [ ] **Step 5: Update job-service PYTHONPATH in integration + branding jobs**

Change:

```yaml
PYTHONPATH: ../agents
```

to:

```yaml
PYTHONPATH: ..:../agents
```

(when cwd is `backend/agents`), and update the comments to say `shared.postgres` lives under `backend/shared/`.

- [ ] **Step 6: Update lint job if it lists directories**

```yaml
ruff check agents/ shared/ unified_api/ blogging_service/ team_service/
```

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
Point CI at backend/shared packages.

EOF
)"
```

---

### Task 5: Orientation docs and package READMEs

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/ARCHITECTURE.md` (shared infra mentions)
- Modify: `docs/ENV_VARS.md` (paths to observability/postgres)
- Modify: `backend/agents/README.md`
- Modify: each `backend/shared/*/README.md` (title, import examples, sys.path note)

**Interfaces:**
- Produces: docs that describe `backend/shared/` and `from shared.postgres import …`

- [ ] **Step 1: Update `CLAUDE.md` structure blurb**

Replace the `shared_postgres/` / `shared_temporal/` lines under `backend/agents/` with a sibling entry:

```text
backend/
  shared/                 # Platform infra (postgres, temporal, …) → import shared.*
  agents/
    …
```

Update the Architecture bullet that links `shared_postgres/README.md` to `backend/shared/postgres/README.md` and Pattern A/B module names to `shared.temporal` / `shared.postgres`.

- [ ] **Step 2: Update package READMEs**

For each README under `backend/shared/*/README.md`:

- Title: `# shared.postgres` (etc.)
- Imports: `from shared.postgres import TeamSchema`
- Preconditions: `backend/` on `sys.path` (not `backend/agents`)

- [ ] **Step 3: Grep docs for stale paths**

```bash
rg -n 'agents/shared_|shared_postgres|shared_temporal' CLAUDE.md docs backend/shared backend/agents/README.md
```

Fix remaining living-doc references (historical design specs under `docs/superpowers/specs/` other than this feature may keep old paths unless they are operational docs — prefer updating operational docs only).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/ARCHITECTURE.md docs/ENV_VARS.md backend/agents/README.md backend/shared
git commit -m "$(cat <<'EOF'
Document backend/shared package layout.

EOF
)"
```

---

### Task 6: Verification

**Files:** none new — run commands only; fix any failures found.

- [ ] **Step 1: Leftover-import gate**

```bash
rg -n '^(from|import) shared_' backend --glob '*.py'
# Expected: no matches
```

- [ ] **Step 2: Unit tests for relocated packages (no live Postgres required for neo4j)**

```bash
cd backend
PYTHONPATH=.:agents pytest shared/neo4j/tests/ -v --tb=short -n 4 --cov=shared.neo4j --cov-fail-under=90
PYTHONPATH=.:agents pytest shared/env/tests shared/env_config/tests shared/concurrency/tests \
  shared/sse/tests shared/hitl/tests shared/http/tests shared/app/tests \
  shared/job_event_bus/tests shared/run_thread_registry/tests \
  shared/command_runner/tests shared/agent_invoke/tests \
  -v --tb=short -n 4
```

Expected: PASS (skip any suites that need live services; run those only if local Postgres is up).

- [ ] **Step 3: Postgres package tests if local Postgres is available**

```bash
cd backend
# with POSTGRES_* env set to a reachable instance
PYTHONPATH=.:agents pytest shared/postgres/tests/ -v --tb=short -n 4 --cov=shared.postgres --cov-fail-under=90
```

If Postgres is unavailable, note that CI `test-shared-postgres` is the gate.

- [ ] **Step 4: Smoke register import**

```bash
cd backend && PYTHONPATH=.:agents python -c "from shared.postgres import register_all_team_schemas; print('registry ok')"
```

- [ ] **Step 5: Ruff on shared**

```bash
cd backend && ruff check shared/
```

Expected: clean (or only pre-existing ignores consistent with pyproject).

- [ ] **Step 6: Final commit only if verification forced fixes**

```bash
git add -A && git status
# if fixes: commit -m "Fix shared relocation verification failures."
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|---|---|
| Target `backend/shared/` with prefix dropped | Task 1 |
| Import style `shared.<name>` | Task 2 |
| Hard cutover, no shims | Tasks 2 + 6 grep gate |
| `backend/` on `sys.path` / PYTHONPATH gaps | Tasks 3–4 |
| CI filters, cov, combine rcfile | Task 4 |
| Docker copies for images that previously copied `shared_*` | Task 3 |
| Docs / CLAUDE / READMEs | Task 5 |
| Out of scope: SE `shared/` | Global constraints + rewrite script caution |
| Verification smoke + tests | Task 6 |

No TBD placeholders. Rename map is complete for all 20 packages plus coveragerc.
