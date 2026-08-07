# Agentic API Testing/Pipeline Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract mode, test-chat, and test-pipeline HTTP handlers from `agentic_team_provisioning/api/main.py` into `api/routes/testing.py` + `api/services/testing.py` without changing URL contracts or the `main` monkeypatch surface.

**Architecture:** Mirror slice 1 (teams/conversations). Thin `APIRouter` in `routes/testing.py`; domain bodies and `_find_agent_in_roster` / `_dispatch_pipeline_run` / `_temporal_enabled` in `services/testing.py`. Hub keeps `_test_store` / `_pipeline_runner` / `_store`; services import `main` inside functions. Re-export `_find_agent_in_roster` and `_dispatch_pipeline_run` from `main`. Preserve Temporal vs thread dispatch behavior.

**Tech Stack:** Python 3.10+, FastAPI `APIRouter`, pytest, existing `agent_team_studio.agentic_team_provisioning` package

**Spec:** `docs/superpowers/specs/2026-08-07-agentic-api-testing-pipeline-split-design.md`

## Global Constraints

- Work only in worktree `.worktrees/5709-extract-testing-pipeline-router` on branch `5709-extract-testing-pipeline-router`
- Design-by-Contract docstrings on every new public function/method/module; preserve existing contracts when moving code
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code (mechanical moves; do not lower thresholds)
- No URL path, status-code, or response-model changes
- Preserve Temporal vs thread dispatch (`temporal_owned` flag; no silent downgrade)
- Do not extract assets/forms/processes/jobs/questions/agent-envs
- Keep `monkeypatch.setattr(main, …)` working; re-export patched helper names from `main`

## File map

Base path: `backend/agents/agent_team_studio/agentic_team_provisioning/`

| File | Role |
|---|---|
| `api/routes/testing.py` | `APIRouter` for mode + test-chat + test-pipeline |
| `api/services/testing.py` | Handler bodies + `_find_agent_in_roster` + `_temporal_enabled` + `_dispatch_pipeline_run` |
| `api/main.py` | Strip testing sections; re-export helpers; `include_router(_testing_router)` |
| `tests/test_api_router_scaffold.py` | Extend with testing mount + hub wiring probes |

## Hub rewrite recipe

When moving a function body from `main.py` into `services/testing.py`:

1. Keep name, signature, docstring, control flow identical.
2. After the docstring, add:

```python
from agent_team_studio.agentic_team_provisioning.api import main as _main
```

3. Rewrite collaborators:

| Before | After |
|---|---|
| `_store` | `_main._store` |
| `_test_store` | `_main._test_store` |
| `_pipeline_runner` | `_main._pipeline_runner` |
| `_get_team_or_404(...)` | `_main._get_team_or_404(...)` |
| `logger` | module-level `logger = logging.getLogger(__name__)` in the service |
| `_find_agent_in_roster` / `_dispatch_pipeline_run` / `_temporal_enabled` | call via `_main.<name>` after re-export from hub (same pattern as `_roster_agent_from_manifest` in slice 1) so monkeypatches on `main` apply |

4. Do **not** import `main` at module top level in routes/services.

Pytest / ruff:

```bash
PYTEST=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/pytest
RUFF=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff
# cwd: worktree .../backend with PYTHONPATH=.
```

Characterization suite (run often):

```bash
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_chat_session_starter_prompt_errors.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_send_test_chat_message_atomicity.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_rate_test_chat_message_tenancy.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_list_endpoints_team_404.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_temporal_dispatch.py \
  -q
```

---

### Task 1: Scaffold empty testing router and extend mount tests

**Files:**
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/testing.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py` (mount only)
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py`

**Interfaces:**
- Consumes: existing `app`, `_teams_router`, `_conversations_router` mounts
- Produces: `routes.testing.router` (empty); `main._testing_router`; mount via `include_router`

- [ ] **Step 1: Extend scaffold tests to expect testing router (fail first)**

Add to `_EXTRACTED_ROUTE_KEYS` (will fail until Task 2 mounts real routes — for Task 1 only add importability + marker assertions):

In `test_api_router_scaffold.py`:

1. Update `test_teams_and_conversations_routers_importable` → rename or add sibling:

```python
def test_testing_router_importable() -> None:
    from fastapi import APIRouter

    from agent_team_studio.agentic_team_provisioning.api.routes import testing

    assert isinstance(testing.router, APIRouter)


def test_main_exposes_testing_router_marker() -> None:
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.routes import testing

    assert main_mod._testing_router is testing.router
```

Do **not** yet add testing paths to `_EXTRACTED_ROUTE_KEYS` in this task (handlers still on `main` as `@app` routes — paths already registered). Path-key expansion belongs in Task 2 after move (to ensure they stay registered via the testing router).

- [ ] **Step 2: Run new tests — expect FAIL**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py::test_testing_router_importable \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py::test_main_exposes_testing_router_marker \
  -v
```

Expected: FAIL (`ModuleNotFoundError` or missing `_testing_router`).

- [ ] **Step 3: Create empty router and mount**

`api/routes/testing.py`:

```python
"""Agentic team provisioning API — testing mode, test-chat, and test-pipeline endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
```

In `main.py` mount block, add:

```python
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    testing as testing_routes,
)

_testing_router = testing_routes.router
app.include_router(_testing_router)
```

Keep existing teams/conversations mounts.

- [ ] **Step 4: Run scaffold + characterization suite**

Expected: all PASS (empty router adds no paths; handlers still on `@app`).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/testing.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py
git commit -m "$(cat <<'EOF'
Scaffold empty testing API router for mode, chat, and pipeline.

Mount last on the hub beside teams/conversations so handlers can move
without changing include order or the monkeypatch surface.
EOF
)"
```

---

### Task 2: Extract mode, test-chat, and test-pipeline into services/testing

**Files:**
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/services/testing.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/testing.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py` (path keys + `_test_store` wiring probe)

**Interfaces:**
- Consumes: `main._store`, `main._test_store`, `main._pipeline_runner`, `main._get_team_or_404`
- Produces in `services.testing`: all moved handlers + `_find_agent_in_roster` + `_temporal_enabled` + `_dispatch_pipeline_run`
- Produces on `main`: re-exports `_find_agent_in_roster`, `_dispatch_pipeline_run`

**Source range in current `main.py`:** from `# Interactive Testing Mode` (~683) through end of `cancel_pipeline_run` (~1128), including `_temporal_enabled` and `_dispatch_pipeline_run`. Leave `# --- Mount extracted routers` intact.

- [ ] **Step 1: Run characterization suite (green before edit)**

Expected: PASS.

- [ ] **Step 2: Create `api/services/testing.py`**

Module docstring:

```python
"""Testing mode, test-chat, and test-pipeline domain logic for agentic HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers, including Temporal vs
    thread dispatch. Collaborators are read from ``api.main`` at call time.
"""
```

Imports: mirror what the moved handlers need (`uuid`, `logging`, FastAPI `HTTPException`/`Response` if used, models, `build_agent` / `call_agent` / etc. as currently imported in those sections of `main`). Prefer copying import lines from the moved region of `main` and trimming unused after ruff.

Paste every moved function; apply Hub rewrite recipe. Critical patterns:

```python
def _find_agent_in_roster(team_id: str, agent_name: str) -> AgenticTeamAgent:
    """Look up an agent by name in the team roster."""
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    agents = _main._store.list_team_agents(team_id)
    for a in agents:
        if a.agent_name == agent_name:
            return a
    raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found in team roster")


def _dispatch_pipeline_run(..., *, temporal_owned: bool) -> str:
    ...
    from agent_team_studio.agentic_team_provisioning.api import main as _main
    if temporal_owned:
        ...  # unchanged Temporal start
        return "Temporal"
    _main._pipeline_runner.start_run(run_id, team_agents, process_def)
    return "thread"
```

For handlers that called `_find_agent_in_roster` / `_dispatch_pipeline_run` / `_temporal_enabled`, call `_main._find_agent_in_roster` / `_main._dispatch_pipeline_run` / `_main._temporal_enabled` after re-exports exist (write handlers to use `_main.` forms from the start).

- [ ] **Step 3: Wire `api/routes/testing.py`**

Thin delegates for every endpoint in the design table (same paths, response models, status codes as current `@app` decorators). Example:

```python
@router.put("/teams/{team_id}/mode")
def set_team_mode(team_id: str, req: SetTeamModeRequest):
    return testing_svc.set_team_mode(team_id, req)
```

Import request/response models from `agentic_team_provisioning.models` as needed.

- [ ] **Step 4: Strip moved section from `main.py` and re-export**

1. Delete Interactive Testing Mode + Agent Chat Testing + Pipeline Testing sections (~683–1128).
2. Add re-exports near mount block:

```python
from agent_team_studio.agentic_team_provisioning.api.services.testing import (  # noqa: E402,F401
    _dispatch_pipeline_run,  # re-export: hub call surface
    _find_agent_in_roster,  # re-export: tests monkeypatch via main
    _temporal_enabled,  # re-export if anything still needs it via main
)
```

3. Trim unused imports with ruff.
4. Update module docstring remaining-groups list (remove mode/test-chat/test-pipeline from “still live here”).

- [ ] **Step 5: Extend scaffold path keys + hub wiring**

Add to `_EXTRACTED_ROUTE_KEYS`:

```python
("PUT", "/teams/{team_id}/mode"),
("POST", "/teams/{team_id}/test-chat/sessions"),
("GET", "/teams/{team_id}/test-chat/sessions"),
("POST", "/teams/{team_id}/test-pipeline/runs"),
("POST", "/teams/{team_id}/test-pipeline/runs/{run_id}/cancel"),
```

Add:

```python
def test_testing_service_reads_test_store_from_main(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.services import testing as testing_svc
    from agent_team_studio.agentic_team_provisioning.models import SetTeamModeRequest, TeamMode

    class _Boom:
        def set_team_mode(self, *_a, **_k):
            raise RuntimeError("hub-test-store-hit")

    monkeypatch.setattr(main_mod, "_store", type("S", (), {"get_team": lambda self, tid: object()})())
    # Prefer a real team stub if SetTeamModeRequest needs a valid team:
    # Use monkeypatch on _store.get_team returning a simple namespace, and _test_store boom.

    class _Team:
        pass

    monkeypatch.setattr(main_mod, "_store", type("Store", (), {"get_team": lambda self, tid: _Team()})())
    monkeypatch.setattr(main_mod, "_test_store", _Boom())
    with pytest.raises(RuntimeError, match="hub-test-store-hit"):
        testing_svc.set_team_mode(team_id="t1", req=SetTeamModeRequest(mode=TeamMode.testing))
```

Adjust `TeamMode` enum member names to match the model (`testing` / `development` — verify in `models.py` before writing). Fix import formatting with ruff (`I001`).

Also assert `_testing_router` in `test_main_exposes_mounted_router_markers` alongside teams/conversations.

- [ ] **Step 6: Run characterization + temporal dispatch**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_chat_session_starter_prompt_errors.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_send_test_chat_message_atomicity.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_rate_test_chat_message_tenancy.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_list_endpoints_team_404.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_temporal_dispatch.py \
  -q
```

Expected: all PASS. If `_find_agent_in_roster` patch tests fail, re-export is missing or handlers call the local name instead of `_main._find_agent_in_roster`.

- [ ] **Step 7: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/services/testing.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/testing.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py
git commit -m "$(cat <<'EOF'
Extract agentic testing mode, test-chat, and pipeline into routes/services.

Handlers leave the hub; roster lookup and pipeline dispatch re-export from
main so Temporal/thread behavior and monkeypatches stay intact.
EOF
)"
```

---

### Task 3: Hub docstring polish, lint, full package verification

**Files:**
- Modify: `api/main.py` docstring if not fully updated in Task 2

- [ ] **Step 1: Confirm docstring lists testing under extracted concerns**

```python
"""FastAPI application for the Agentic Team Provisioning service.

This module is the app-assembly hub. Extracted concerns live in:

* ``api.routes.teams`` / ``api.services.teams`` — teams CRUD + roster
* ``api.routes.conversations`` / ``api.services.conversations`` — conversations
* ``api.routes.testing`` / ``api.services.testing`` — mode, test-chat, test-pipeline

Remaining endpoint groups (processes, jobs, questions, assets, forms) still
live here pending later splits.

This module remains the owning namespace for collaborators the test suite
monkeypatches (``_store``, ``_agent``, ``_test_store``, ``_pipeline_runner``,
``_find_agent_in_roster``, ``_dispatch_pipeline_run``, …). Route and service
modules dereference those names through ``main`` at call time.
"""
```

- [ ] **Step 2: Assert no inline testing handlers remain**

```bash
cd backend/agents/agent_team_studio/agentic_team_provisioning
rg -n '@app\.(get|post|put|delete)\("/teams/\{team_id\}/(mode|test-)' api/main.py
# Expect: no matches
rg -n 'def _dispatch_pipeline_run|def _find_agent_in_roster|def create_test_chat_session|def start_pipeline_run' api/main.py
# Expect: no definitions (re-exports are imports only)
```

- [ ] **Step 3: Ruff**

```bash
cd backend
"$RUFF" check --fix \
  agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  agents/agent_team_studio/agentic_team_provisioning/api/routes \
  agents/agent_team_studio/agentic_team_provisioning/api/services \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py
"$RUFF" format \
  agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  agents/agent_team_studio/agentic_team_provisioning/api/routes \
  agents/agent_team_studio/agentic_team_provisioning/api/services \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py
```

- [ ] **Step 4: Full package tests**

```bash
cd backend
PYTHONPATH=. "$PYTEST" agents/agent_team_studio/agentic_team_provisioning/tests/ -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py
git commit -m "$(cat <<'EOF'
Document agentic API hub after testing/pipeline extraction.

List the testing router/service among extracted concerns and keep main as
the monkeypatch surface for test-store and pipeline dispatch.
EOF
)"
```

If docstring was already updated in Task 2 and only ruff touched files, amend commit message scope or fold ruff into Task 2’s commit instead of an empty Task 3 commit — do not create an empty commit.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `routes/testing.py` + `services/testing.py` | 1, 2 |
| Mode + test-chat + test-pipeline moved | 2 |
| Pipeline contracts / Temporal vs thread | 2 |
| Hub dereference + re-exports | 2 |
| Scaffold mount/wiring | 1, 2 |
| Related tests pass | 2, 3 |
| Assets/forms out of scope | all |

## Plan self-review notes

- Hub rewrite matches slice 1; `_find_agent_in_roster` patch target called out.
- `_temporal_enabled` moves with dispatch (only consumer).
- No TBD steps; TeamMode enum member must be verified against `models.py` when writing the wiring probe.
