# Agentic API Teams/Conversations Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract teams CRUD + roster and conversation HTTP handlers from `agentic_team_provisioning/api/main.py` into `api/routes/*` + `api/services/*` without changing URL contracts or the `main` monkeypatch surface.

**Architecture:** Follow branding’s thin-hub pattern with an explicit `api/services/` package. Route modules own `APIRouter` wiring; service modules own handler bodies. Collaborators (`_store`, `_agent`, `_save_agents_from_llm`, …) stay on `main` and are read via function-local `from agent_team_studio.agentic_team_provisioning.api import main as _main`. Roster-only helpers move into `services/teams.py` and are re-exported from `main` so existing `monkeypatch` / direct imports keep working. Processes, assets, forms, test-chat, and test-pipeline stay inline until later slices.

**Tech Stack:** Python 3.10+, FastAPI `APIRouter`, pytest, existing `agent_team_studio.agentic_team_provisioning` package

**Spec:** `docs/superpowers/specs/2026-08-07-agentic-api-teams-conversations-split-design.md`

## Global Constraints

- Work only in worktree `.worktrees/5708-extract-teams-conversations-routers` on branch `5708-extract-teams-conversations-routers`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function/method/module; preserve existing contracts when moving code
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code (mechanical moves of already-covered handlers; do not lower thresholds)
- No URL path, status-code, or response-model changes
- Do not extract processes, jobs, questions, agent-envs, assets, forms, mode, test-chat, or test-pipeline in this plan
- Keep `monkeypatch.setattr(main, …)` working; re-export patched helper names from `main`

## File map

Base path: `backend/agents/agent_team_studio/agentic_team_provisioning/`

| File | Role |
|---|---|
| `api/routes/__init__.py` | Package docstring (branding-style) |
| `api/routes/teams.py` | `APIRouter` for teams CRUD + roster endpoints |
| `api/routes/conversations.py` | `APIRouter` for conversation endpoints |
| `api/services/__init__.py` | Package docstring |
| `api/services/teams.py` | Team/roster handler bodies + roster helpers |
| `api/services/conversations.py` | Conversation handler bodies + `_build_state_response` |
| `api/main.py` | Keep globals + remaining inline routes; strip moved handlers; re-export roster helpers; `include_router` last |
| `tests/test_api_router_scaffold.py` | Import/mount smoke test for new packages (Task 1) |

## Hub rewrite recipe (apply in Tasks 2–3)

When moving a function body out of `main.py` into a service:

1. Keep the function name, signature, docstring, and control flow identical.
2. At the **start** of the function body (after the docstring), add:

```python
from agent_team_studio.agentic_team_provisioning.api import main as _main
```

3. Rewrite every hub collaborator reference:

| Before (in `main`) | After (in service) |
|---|---|
| `_store` | `_main._store` |
| `_agent` | `_main._agent` |
| `_save_agents_from_llm(...)` | `_main._save_agents_from_llm(...)` |
| `_after_process_saved(...)` | `_main._after_process_saved(...)` |
| `logger` | module-level `logger = logging.getLogger(__name__)` in the service file |
| `_roster_agent_from_manifest` / `_unregister_*` / `_reregister_*` / `_generated_manifest_cleanup` | keep local same-module names |
| `_build_state_response` | keep local same-module name in conversations service |

4. Nested closures that previously closed over `_store` / helpers must close over `_main._store` / local helpers after the rewrite.
5. Do **not** import `main` at module top level in `routes/*` or `services/*`.

Pytest binary for all commands below:

```bash
PYTEST=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/pytest
RUFF=/Users/brandonkind/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff
# cwd: worktree .../backend with PYTHONPATH=.
```

---

### Task 1: Scaffold routes/services packages and mount empty routers

**Files:**
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/__init__.py`
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/teams.py`
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/conversations.py`
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/services/__init__.py`
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py` (mount routers at end only)

**Interfaces:**
- Consumes: existing `app` from `create_team_app` in `main`
- Produces: `routes.teams.router`, `routes.conversations.router` mounted on `main.app`; `main._teams_router` / `main._conversations_router` aliases

- [ ] **Step 1: Write the failing scaffold test**

Create `tests/test_api_router_scaffold.py`:

```python
"""Smoke: teams/conversations router packages exist and main mounts them."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.routing import APIRoute


def test_teams_and_conversations_routers_importable() -> None:
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams

    assert isinstance(teams.router, APIRouter)
    assert isinstance(conversations.router, APIRouter)


def test_main_exposes_mounted_router_markers() -> None:
    """main keeps explicit references so we can assert include_router ran."""
    from agent_team_studio.agentic_team_provisioning.api import main as main_mod
    from agent_team_studio.agentic_team_provisioning.api.routes import conversations, teams

    assert main_mod._teams_router is teams.router
    assert main_mod._conversations_router is conversations.router
    paths = {getattr(r, "path", None) for r in main_mod.app.routes if isinstance(r, APIRoute)}
    assert "/health" in paths
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py -v
```

Expected: FAIL with `ModuleNotFoundError` for `api.routes` (or missing `_teams_router`).

- [ ] **Step 3: Create packages and empty routers**

`api/routes/__init__.py`:

```python
"""Concern-grouped ``APIRouter`` modules for agentic team provisioning HTTP.

Each module declares a bare ``router = APIRouter()``; ``api.main`` imports them
last (after the app + shared globals are defined) and mounts them with
``app.include_router(...)``. Route paths stay absolute and unchanged from the
pre-split monolith.

Handlers / services dereference monkeypatched collaborators through
``from agent_team_studio.agentic_team_provisioning.api import main as _main`` at
call time so ``monkeypatch.setattr(main, …)`` keeps working.
"""
```

`api/services/__init__.py`:

```python
"""Domain service modules for agentic team provisioning HTTP handlers.

Routers in ``api.routes`` stay thin; business logic for extracted endpoint
groups lives here. Services import ``api.main`` only inside functions to avoid
import cycles and to honor the hub monkeypatch surface.
"""
```

`api/routes/teams.py`:

```python
"""Agentic team provisioning API — teams CRUD + roster endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
```

`api/routes/conversations.py`:

```python
"""Agentic team provisioning API — conversation endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
```

- [ ] **Step 4: Mount routers at the end of `main.py`**

Append after all existing route definitions:

```python
# --- Mount extracted routers last (hub + globals already defined) ---
from agent_team_studio.agentic_team_provisioning.api.routes import (  # noqa: E402
    conversations as conversations_routes,
    teams as teams_routes,
)

_teams_router = teams_routes.router
_conversations_router = conversations_routes.router
app.include_router(_teams_router)
app.include_router(_conversations_router)
```

Do **not** remove any handlers yet.

- [ ] **Step 5: Run scaffold + baseline teams/conversation tests**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_create_team_rollback.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_registry_roster.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_agent_manifests_endpoint.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_conversation_registry_failure.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_set_conversation_process.py \
  -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/__init__.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/teams.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/conversations.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/services/__init__.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py
git commit -m "$(cat <<'EOF'
Scaffold agentic API routes/services packages for teams and conversations.

Empty routers mount last on the hub so later slices can move handlers without
changing include order or the monkeypatch surface.
EOF
)"
```

---

### Task 2: Extract teams CRUD + roster into services/teams + routes/teams

**Files:**
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/services/teams.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/teams.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py`

**Interfaces:**
- Consumes: `main._store`; models/helpers used by current team/roster handlers
- Produces in `services.teams`: `create_team`, `list_teams`, `get_team`, `list_team_agents`, `list_team_agent_manifests`, `validate_team_roster`, `add_agent_from_registry`, `remove_agent_from_roster`, `update_roster_agent`, `_roster_agent_from_manifest`, `_unregister_generated_manifest`, `_reregister_generated_manifest`, `_generated_manifest_cleanup`
- Produces on `main`: re-export `_roster_agent_from_manifest` (tests import + monkeypatch it)

**Source ranges in current `main.py` (cut verbatim, then apply Hub rewrite recipe):**

| Symbol | Approx lines |
|---|---|
| `create_team` … `get_team` | 267–338 |
| `list_team_agents` … `validate_team_roster` | 346–424 |
| `_roster_agent_from_manifest` … `_generated_manifest_cleanup` | 427–556 |
| `add_agent_from_registry` … `update_roster_agent` | 559–707 |

Leave the Processes section untouched. Keep `_save_agents_from_llm` / `_after_process_saved` on `main`.

- [ ] **Step 1: Run characterization tests (green before edit)**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_create_team_rollback.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_registry_roster.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_agent_manifests_endpoint.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Create `api/services/teams.py` with moved bodies**

Header:

```python
"""Team CRUD and roster domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers (status codes, bodies,
    registry side effects). Collaborators are read from ``api.main`` at call
    time so tests can ``monkeypatch.setattr(main, …)``.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import HTTPException, Response
from pydantic import ValidationError

from agent_registry.models import AgentManifest
from agent_team_studio.agentic_team_provisioning.infrastructure import provision_team
from agent_team_studio.agentic_team_provisioning.manifest_generation import (
    build_agent_manifest,
    is_generated_manifest,
)
from agent_team_studio.agentic_team_provisioning.models import (
    SOURCE_GENERATED,
    SOURCE_REGISTRY,
    AddAgentFromRegistryRequest,
    AgenticTeamAgent,
    CreateTeamRequest,
    CreateTeamResponse,
    GeneratedManifestsResponse,
    RosterValidationResult,
    TeamDetailResponse,
    TeamSummary,
    UpdateAgentRequest,
)

logger = logging.getLogger(__name__)
```

Paste each moved function and apply the Hub rewrite recipe. Example for `create_team`:

```python
def create_team(req: CreateTeamRequest):
    """Create a new agentic team and provision its infrastructure.

    Persists the team row first, then provisions the team's infrastructure.
    If provisioning fails, the team row is rolled back via ``_store.delete_team``
    so no orphaned, infrastructure-less row survives a failed create.

    Preconditions: ``req.name`` is a non-empty team name (enforced by
        ``CreateTeamRequest``).
    Postconditions: on success, the team row exists, infrastructure is
        provisioned, and ``200`` is returned with the created team. On
        provisioning failure, the team row is removed (best-effort — a
        rollback failure is logged but never masks the ``500``) and an
        ``HTTPException(500)`` is raised.
    Invariants: when provisioning fails and the compensating delete succeeds,
        no team row remains in Postgres without corresponding infrastructure.
        The delete is best-effort, so a rollback failure is logged but the row
        may remain — see Postconditions.
    """
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    team = _main._store.create_team(name=req.name, description=req.description)
    try:
        provision_team(team.team_id)
    except Exception as exc:
        logger.exception(
            "Failed to provision infrastructure for team %s; rolling back team row",
            team.team_id,
        )
        try:
            _main._store.delete_team(team.team_id)
        except Exception:
            logger.exception(
                "Failed to roll back team row for team %s after provisioning failure",
                team.team_id,
            )
        raise HTTPException(
            status_code=500, detail="Failed to provision team infrastructure."
        ) from exc
    return CreateTeamResponse(
        team_id=team.team_id,
        name=team.name,
        description=team.description,
        created_at=team.created_at,
    )
```

Roster helpers (`_roster_agent_from_manifest`, unregister/reregister/cleanup): move without `_main` for pure projection; keep inline `get_registry` imports. Handlers that called those helpers keep same-module calls.

- [ ] **Step 3: Wire `api/routes/teams.py`**

```python
"""Agentic team provisioning API — teams CRUD + roster endpoints.

Handlers delegate to ``api.services.teams`` so business logic stays out of the router.
"""

from __future__ import annotations

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import teams as teams_svc
from agent_team_studio.agentic_team_provisioning.models import (
    AddAgentFromRegistryRequest,
    AgenticTeamAgent,
    CreateTeamRequest,
    CreateTeamResponse,
    GeneratedManifestsResponse,
    RosterValidationResult,
    TeamDetailResponse,
    TeamSummary,
    UpdateAgentRequest,
)

router = APIRouter()


@router.post("/teams", response_model=CreateTeamResponse)
def create_team(req: CreateTeamRequest):
    return teams_svc.create_team(req)


@router.get("/teams", response_model=list[TeamSummary])
def list_teams():
    return teams_svc.list_teams()


@router.get("/teams/{team_id}", response_model=TeamDetailResponse)
def get_team(team_id: str):
    return teams_svc.get_team(team_id)


@router.get("/teams/{team_id}/agents", response_model=list[AgenticTeamAgent])
def list_team_agents(team_id: str):
    return teams_svc.list_team_agents(team_id)


@router.get("/teams/{team_id}/agents/manifests", response_model=GeneratedManifestsResponse)
def list_team_agent_manifests(team_id: str):
    return teams_svc.list_team_agent_manifests(team_id)


@router.get("/teams/{team_id}/roster/validation", response_model=RosterValidationResult)
def validate_team_roster(team_id: str):
    return teams_svc.validate_team_roster(team_id)


@router.post("/teams/{team_id}/agents/from-registry", response_model=AgenticTeamAgent, status_code=201)
def add_agent_from_registry(team_id: str, req: AddAgentFromRegistryRequest):
    return teams_svc.add_agent_from_registry(team_id, req)


@router.delete("/teams/{team_id}/agents/{agent_name:path}", status_code=204)
def remove_agent_from_roster(team_id: str, agent_name: str):
    return teams_svc.remove_agent_from_roster(team_id, agent_name)


@router.put("/teams/{team_id}/agents/{agent_name:path}", response_model=AgenticTeamAgent)
def update_roster_agent(team_id: str, agent_name: str, req: UpdateAgentRequest):
    return teams_svc.update_roster_agent(team_id, agent_name, req)
```

- [ ] **Step 4: Strip moved code from `main.py` and re-export**

1. Delete the Teams / Team agents pool sections that were moved (handlers + roster helpers), leaving Health above and Processes below.
2. With the router mounts, add:

```python
from agent_team_studio.agentic_team_provisioning.api.services.teams import (  # noqa: E402,F401
    _roster_agent_from_manifest,  # re-export: tests import + monkeypatch via main
)
```

3. Trim unused imports only when ruff complains.

- [ ] **Step 5: Run teams tests**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_create_team_rollback.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_registry_roster.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_agent_manifests_endpoint.py \
  -q
```

Expected: all PASS. If `test_roster_agent_from_manifest_*` fail on import, the `main` re-export is missing.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/services/teams.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/teams.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py
git commit -m "$(cat <<'EOF'
Extract agentic teams CRUD and roster into routes and services.

Handlers leave the hub module; roster helpers re-export from main so existing
monkeypatches and direct imports keep working.
EOF
)"
```

---

### Task 3: Extract conversations into services/conversations + routes/conversations

**Files:**
- Create: `backend/agents/agent_team_studio/agentic_team_provisioning/api/services/conversations.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/conversations.py`
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py`

**Interfaces:**
- Consumes: `main._store`, `main._agent`, `main._save_agents_from_llm`, `main._after_process_saved`; `GREETING` / `DEFAULT_SUGGESTIONS` from their defining module (or `_main` if only bound there)
- Produces: `_build_state_response`, `create_conversation`, `send_message`, `set_conversation_process`, `get_conversation`, `list_conversations`

Find the conversation section by comment `# Conversations` (line numbers shift after Task 2).

- [ ] **Step 1: Run conversation characterization tests**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_conversation_registry_failure.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_set_conversation_process.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Create `api/services/conversations.py`**

```python
"""Conversation domain logic for agentic team provisioning HTTP.

Preconditions: callers pass the same request models / ids the former ``main``
    handlers accepted.
Postconditions: behavior matches the pre-split handlers, including 503 on
    registry failure after a persisted chat turn. Collaborators are read from
    ``api.main`` at call time.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from agent_team_studio.agentic_team_provisioning.models import (
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    ProcessDefinition,
    SendMessageRequest,
    SetConversationProcessRequest,
)

logger = logging.getLogger(__name__)


def _build_state_response(
    conversation_id: str,
    team_id: str,
    process: Optional[ProcessDefinition],
    suggested_questions: list[str],
) -> ConversationStateResponse:
    from agent_team_studio.agentic_team_provisioning.api import main as _main

    messages = _main._store.get_messages(conversation_id)
    return ConversationStateResponse(
        conversation_id=conversation_id,
        team_id=team_id,
        messages=messages,
        current_process=process,
        suggested_questions=suggested_questions,
    )
```

Move `create_conversation`, `send_message`, `set_conversation_process`, `get_conversation`, and `list_conversations` with the Hub rewrite recipe. Critical substitutions:

- `_store` → `_main._store`
- `_agent` → `_main._agent`
- `_save_agents_from_llm(...)` → `_main._save_agents_from_llm(...)`
- `_after_process_saved(...)` → `_main._after_process_saved(...)`
- `_build_state_response(...)` → local `_build_state_response(...)`

Registry-failure fragment:

```python
        try:
            _main._save_agents_from_llm(req.team_id, agents_data)
        except Exception as e:
            logger.warning(
                "Roster save failed for team %s after conversation %s turn: %s",
                req.team_id,
                conversation_id,
                e,
            )
            raise HTTPException(status_code=503, detail="Agent registry unavailable") from e
```

- [ ] **Step 3: Wire `api/routes/conversations.py`**

```python
"""Agentic team provisioning API — conversation endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from agent_team_studio.agentic_team_provisioning.api.services import conversations as conv_svc
from agent_team_studio.agentic_team_provisioning.models import (
    ConversationStateResponse,
    ConversationSummaryResponse,
    CreateConversationRequest,
    SendMessageRequest,
    SetConversationProcessRequest,
)

router = APIRouter()


@router.post("/conversations", response_model=ConversationStateResponse)
def create_conversation(req: CreateConversationRequest):
    return conv_svc.create_conversation(req)


@router.post("/conversations/{conversation_id}/messages", response_model=ConversationStateResponse)
def send_message(conversation_id: str, req: SendMessageRequest):
    return conv_svc.send_message(conversation_id, req)


@router.put("/conversations/{conversation_id}/process")
def set_conversation_process(conversation_id: str, req: SetConversationProcessRequest):
    return conv_svc.set_conversation_process(conversation_id, req)


@router.get("/conversations/{conversation_id}", response_model=ConversationStateResponse)
def get_conversation(conversation_id: str):
    return conv_svc.get_conversation(conversation_id)


@router.get("/teams/{team_id}/conversations", response_model=list[ConversationSummaryResponse])
def list_conversations(team_id: str):
    return conv_svc.list_conversations(team_id)
```

- [ ] **Step 4: Strip conversation section from `main.py`**

Delete `_build_state_response` and all five conversation route handlers. Do **not** delete `_save_agents_from_llm` or `_after_process_saved`.

- [ ] **Step 5: Run conversation + teams regression**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_api_router_scaffold.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_create_team_rollback.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_registry_roster.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_agent_manifests_endpoint.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_conversation_registry_failure.py \
  agents/agent_team_studio/agentic_team_provisioning/tests/test_set_conversation_process.py \
  -q
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/services/conversations.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/routes/conversations.py \
  backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py
git commit -m "$(cat <<'EOF'
Extract agentic conversation endpoints into routes and services.

Chat handlers leave the hub; roster save and process-provision hooks still
resolve through main so registry-failure monkeypatches keep working.
EOF
)"
```

---

### Task 4: Hub docstring, lint, and full package verification

**Files:**
- Modify: `backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py`

**Interfaces:**
- Consumes: Tasks 1–3 layout
- Produces: accurate module docstring describing hub vs routers/services

- [ ] **Step 1: Update `main.py` module docstring**

```python
"""FastAPI application for the Agentic Team Provisioning service.

This module is the app-assembly hub. Extracted concerns live in:

* ``api.routes.teams`` / ``api.services.teams`` — teams CRUD + roster
* ``api.routes.conversations`` / ``api.services.conversations`` — conversations

Remaining endpoint groups (processes, jobs, questions, assets, forms, mode,
test-chat, test-pipeline) still live here pending later splits.

This module remains the owning namespace for collaborators the test suite
monkeypatches (``_store``, ``_agent``, ``_test_store``, ``_pipeline_runner``,
``_save_agents_from_llm``, ``_roster_agent_from_manifest``, …). Route and
service modules dereference those names through ``main`` at call time.
"""
```

- [ ] **Step 2: Assert no inline teams/conversation handlers remain**

```bash
cd backend/agents/agent_team_studio/agentic_team_provisioning
rg -n '@app\.(get|post|put|delete)\("/conversations' api/main.py
# Expect: no matches
rg -n 'def create_team\(|def list_team_agents\(|def create_conversation\(|def send_message\(' api/main.py
# Expect: no matches
rg -n '@app\.(post|get)\("/teams"' api/main.py
# Expect: no matches for bare /teams CRUD (suffix routes like /processes may remain)
```

- [ ] **Step 3: Ruff check on touched files**

```bash
cd backend
"$RUFF" check \
  agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  agents/agent_team_studio/agentic_team_provisioning/api/routes \
  agents/agent_team_studio/agentic_team_provisioning/api/services
"$RUFF" format \
  agents/agent_team_studio/agentic_team_provisioning/api/main.py \
  agents/agent_team_studio/agentic_team_provisioning/api/routes \
  agents/agent_team_studio/agentic_team_provisioning/api/services
```

Expected: clean.

- [ ] **Step 4: Run full agentic_team_provisioning test package**

```bash
cd backend
PYTHONPATH=. "$PYTEST" \
  agents/agent_team_studio/agentic_team_provisioning/tests/ -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/agent_team_studio/agentic_team_provisioning/api/main.py
git commit -m "$(cat <<'EOF'
Document agentic API hub after teams and conversations extraction.

Clarify which routers/services own those concerns and that main remains the
monkeypatch surface for shared collaborators.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `api/routes/teams.py` + `conversations.py` exist and mount from `main` | 1, 2, 3 |
| `api/services/teams.py` + `conversations.py` own moved logic | 2, 3 |
| Teams scope = CRUD + roster | 2 |
| Conversations endpoints listed in spec | 3 |
| `main` has no inline handlers for those groups | 2, 3, 4 |
| Hub dereference / no top-level `main` import in services | 2, 3 (recipe) |
| `_roster_agent_from_manifest` re-exported from `main` | 2 |
| `_save_agents_from_llm` stays on `main` | 2, 3 |
| Processes/assets/forms/test-* stay inline | all (explicit non-goals) |
| Existing teams/conversation tests pass | 2, 3, 4 |
| No URL contract changes | 2, 3 |
| Docs deferred | no task (by design) |

## Plan self-review notes

- No TBD/placeholder steps; hub rewrite recipe is the shared transform for large moves.
- Types/names align with current `main.py` and the design spec.
- Scaffold test uses `_teams_router` / `_conversations_router` markers so empty-router include is assertable.
