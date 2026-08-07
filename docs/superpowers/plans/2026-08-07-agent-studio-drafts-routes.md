# Agent Studio Drafts HTTP Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `/api/agent-studio/drafts` CRUD (create, put-update, list summaries, get full, rename, delete) over `get_draft_store()`, scoped via a pluggable user-id dependency.

**Architecture:** Extend the existing `agent_studio` FastAPI router with sync handlers that call the drafts store. `get_current_user_id()` defaults to `DEFAULT_USER_ID` and is overridable in tests. Create is `POST`; full update is `PUT` (intentional UX divergence). Opaque `{name?, payload?}` request envelope.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic, pytest + TestClient, existing `AgentStudioDraftStore`

## Global Constraints

- Work only in worktree `.worktrees/5701-agent-studio-drafts-routes` on branch `feature/5701-agent-studio-drafts-routes`
- Spec: `docs/superpowers/specs/2026-08-07-agent-studio-drafts-routes-design.md`
- Design-by-Contract docstrings on every new public function/method
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- OpenAPI tag remains `agent-studio`
- `ValueError` → 400; store `None`/`False` → 404
- No Angular, Temporal wrappers, or `?q=` filter

## File map

| File | Role |
|---|---|
| `agent_studio/models.py` | Add `SaveDraftRequest`, `RenameDraftRequest` |
| `unified_api/routes/agent_studio.py` | `get_current_user_id` + six drafts endpoints |
| `unified_api/tests/test_agent_studio_drafts_routes.py` | Hermetic TestClient suite |

---

### Task 1: Draft request models

**Files:**
- Modify: `backend/agents/agent_team_studio/agent_studio/models.py`
- Test: `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py` (create)

**Interfaces:**
- Consumes: existing `BaseModel`, `Field`, `Any`
- Produces:
  - `SaveDraftRequest(name: str | None = None, payload: dict[str, Any] | None = None)`
  - `RenameDraftRequest(name: str)` with `min_length=1`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py`:

```python
"""Unit tests for drafts HTTP request models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_team_studio.agent_studio.models import RenameDraftRequest, SaveDraftRequest


def test_save_draft_request_defaults() -> None:
    req = SaveDraftRequest()
    assert req.name is None
    assert req.payload is None


def test_save_draft_request_accepts_name_and_payload() -> None:
    req = SaveDraftRequest(name="Alpha", payload={"teamId": "t1"})
    assert req.name == "Alpha"
    assert req.payload == {"teamId": "t1"}


def test_rename_draft_request_requires_nonempty_name() -> None:
    assert RenameDraftRequest(name="Renamed").name == "Renamed"
    with pytest.raises(ValidationError):
        RenameDraftRequest(name="")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py -v
```

Expected: FAIL — cannot import `SaveDraftRequest` / `RenameDraftRequest`

- [ ] **Step 3: Add the models**

Append to `backend/agents/agent_team_studio/agent_studio/models.py` after `AgentStudioDraft`:

```python
class SaveDraftRequest(BaseModel):
    """Create/update body: optional label + opaque stage/handoff payload.

    Invariants:
        * ``payload``, when provided, is a JSON object (``dict``); the store
          rejects non-dicts with ``ValueError``.
    """

    name: str | None = None
    payload: dict[str, Any] | None = None


class RenameDraftRequest(BaseModel):
    """Rename body for ``PATCH /drafts/{draft_id}``."""

    name: str = Field(..., min_length=1)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/agent_team_studio/agent_studio/models.py \
  backend/agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py
git commit -m "$(cat <<'EOF'
Add SaveDraftRequest and RenameDraftRequest models.

Opaque envelope keeps stage/handoff fields out of the HTTP contract.

EOF
)"
```

---

### Task 2: Drafts routes + hermetic API tests

**Files:**
- Modify: `backend/unified_api/routes/agent_studio.py`
- Create: `backend/unified_api/tests/test_agent_studio_drafts_routes.py`

**Interfaces:**
- Consumes: `get_draft_store`, `SaveDraftRequest`, `RenameDraftRequest`, `AgentStudioDraft`, `AgentStudioDraftSummary`, `DEFAULT_USER_ID`
- Produces:
  - `get_current_user_id() -> str`
  - `POST /api/agent-studio/drafts` → summary
  - `PUT /api/agent-studio/drafts/{draft_id}` → summary
  - `GET /api/agent-studio/drafts` → `list[AgentStudioDraftSummary]`
  - `GET /api/agent-studio/drafts/{draft_id}` → `AgentStudioDraft`
  - `PATCH /api/agent-studio/drafts/{draft_id}` → summary
  - `DELETE /api/agent-studio/drafts/{draft_id}` → `{draft_id, status: "deleted"}`

- [ ] **Step 1: Write the failing route tests**

Create `backend/unified_api/tests/test_agent_studio_drafts_routes.py`:

```python
"""Hermetic TestClient tests for Agent Studio drafts HTTP routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_team_studio.agent_studio.drafts_store import AgentStudioDraftStore
from unified_api.routes import agent_studio as routes


@pytest.fixture()
def store() -> AgentStudioDraftStore:
    return AgentStudioDraftStore()


@pytest.fixture()
def client(store: AgentStudioDraftStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(routes, "get_draft_store", lambda: store)
    app = FastAPI()
    app.include_router(routes.router)
    # Default user is whatever get_current_user_id returns; override per-test when needed.
    return TestClient(app)


def _as_user(client: TestClient, user_id: str) -> None:
    client.app.dependency_overrides[routes.get_current_user_id] = lambda: user_id


def test_create_list_get_round_trip(client: TestClient) -> None:
    _as_user(client, "alice")
    created = client.post("/api/agent-studio/drafts", json={"name": "Alpha", "payload": {"teamId": "t1"}})
    assert created.status_code == 200
    summary = created.json()
    assert summary["name"] == "Alpha"
    assert "draft_id" in summary and "updated_at" in summary
    assert "payload" not in summary

    listed = client.get("/api/agent-studio/drafts")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["draft_id"] == summary["draft_id"]

    full = client.get(f"/api/agent-studio/drafts/{summary['draft_id']}")
    assert full.status_code == 200
    body = full.json()
    assert body["payload"] == {"teamId": "t1"}
    assert body["created_at"]


def test_put_updates_owned_draft(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Old"}).json()["draft_id"]
    updated = client.put(
        f"/api/agent-studio/drafts/{draft_id}",
        json={"name": "New", "payload": {"a": 2}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "New"
    full = client.get(f"/api/agent-studio/drafts/{draft_id}").json()
    assert full["payload"] == {"a": 2}


def test_put_missing_returns_404(client: TestClient) -> None:
    _as_user(client, "alice")
    resp = client.put("/api/agent-studio/drafts/missing", json={"name": "x"})
    assert resp.status_code == 404


def test_rename_and_delete(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Old"}).json()["draft_id"]
    renamed = client.patch(f"/api/agent-studio/drafts/{draft_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Renamed"
    deleted = client.delete(f"/api/agent-studio/drafts/{draft_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"draft_id": draft_id, "status": "deleted"}
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").status_code == 404


def test_rename_empty_name_returns_422_or_400(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post("/api/agent-studio/drafts", json={"name": "Old"}).json()["draft_id"]
    resp = client.patch(f"/api/agent-studio/drafts/{draft_id}", json={"name": ""})
    assert resp.status_code in (400, 422)


def test_tenancy_isolation(client: TestClient) -> None:
    _as_user(client, "alice")
    draft_id = client.post(
        "/api/agent-studio/drafts", json={"name": "Secret", "payload": {"x": 1}}
    ).json()["draft_id"]

    _as_user(client, "bob")
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").status_code == 404
    assert client.put(f"/api/agent-studio/drafts/{draft_id}", json={"name": "Hijack"}).status_code == 404
    assert client.patch(f"/api/agent-studio/drafts/{draft_id}", json={"name": "Hijack"}).status_code == 404
    assert client.delete(f"/api/agent-studio/drafts/{draft_id}").status_code == 404
    assert client.get("/api/agent-studio/drafts").json() == []

    _as_user(client, "alice")
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").status_code == 200
    assert client.get(f"/api/agent-studio/drafts/{draft_id}").json()["name"] == "Secret"


def test_list_pagination_query_params(client: TestClient) -> None:
    _as_user(client, "alice")
    for i in range(3):
        client.post("/api/agent-studio/drafts", json={"name": f"d{i}"})
    page = client.get("/api/agent-studio/drafts", params={"limit": 1, "offset": 1})
    assert page.status_code == 200
    assert len(page.json()) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest unified_api/tests/test_agent_studio_drafts_routes.py -v
```

Expected: FAIL — `get_draft_store` / drafts routes not defined on the router (404s or AttributeError)

- [ ] **Step 3: Implement auth dependency + routes**

Update `backend/unified_api/routes/agent_studio.py`:

1. Expand the module docstring to list the drafts endpoints and note the user-id dependency.
2. Add imports and helpers/handlers. Full replacement of the import + router section additions:

```python
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from agent_team_studio.agent_studio.drafts_runtime import get_draft_store
from agent_team_studio.agent_studio.models import (
    AgentDefinition,
    AgentStudioDraft,
    AgentStudioDraftSummary,
    ConversationStateResponse,
    RenameDraftRequest,
    SaveAgentRequest,
    SaveAgentResponse,
    SaveDraftRequest,
    SendMessageRequest,
    StartConversationRequest,
)
from agent_team_studio.agent_studio.temporal import dispatch
from user_profile.store import DEFAULT_USER_ID
```

Add after `router = APIRouter(...)`:

```python
def get_current_user_id() -> str:
    """Resolve the caller user id for drafts tenancy.

    Postconditions:
        * Returns a non-empty user id. Default is ``DEFAULT_USER_ID`` until real
          auth is wired; tests override via ``app.dependency_overrides``.
    """
    return DEFAULT_USER_ID


def _summary_from_draft(draft: AgentStudioDraft) -> AgentStudioDraftSummary:
    return AgentStudioDraftSummary(
        draft_id=draft.draft_id, name=draft.name, updated_at=draft.updated_at
    )


@router.post("/drafts", response_model=AgentStudioDraftSummary)
def create_draft(
    req: SaveDraftRequest,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraftSummary:
    """Create a new Studio draft owned by the current user.

    Preconditions:
        * ``req`` is FastAPI-validated; ``user_id`` is non-empty from the dependency.
    Postconditions:
        * Returns a summary for the new draft. ``ValueError`` → 400.
    """
    try:
        draft = get_draft_store().create(user_id, name=req.name, payload=req.payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _summary_from_draft(draft)


@router.put("/drafts/{draft_id}", response_model=AgentStudioDraftSummary)
def update_draft(
    draft_id: str,
    req: SaveDraftRequest,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraftSummary:
    """Replace name/payload on an owned draft.

    Postconditions:
        * Returns updated summary, or 404 when missing/wrong user. ``ValueError`` → 400.
    """
    try:
        draft = get_draft_store().update(
            user_id, draft_id, name=req.name, payload=req.payload
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return _summary_from_draft(draft)


@router.get("/drafts", response_model=list[AgentStudioDraftSummary])
def list_drafts(
    user_id: Annotated[str, Depends(get_current_user_id)],
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> list[AgentStudioDraftSummary]:
    """List draft summaries for the current user (most recent first).

    Postconditions:
        * Returns summaries only; store clamps ``limit`` to max 100.
    """
    return get_draft_store().list_summaries(user_id, limit=limit, offset=offset)


@router.get("/drafts/{draft_id}", response_model=AgentStudioDraft)
def get_draft(
    draft_id: str,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraft:
    """Load the full draft payload for the current user."""
    draft = get_draft_store().get(user_id, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return draft


@router.patch("/drafts/{draft_id}", response_model=AgentStudioDraftSummary)
def rename_draft(
    draft_id: str,
    req: RenameDraftRequest,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> AgentStudioDraftSummary:
    """Rename an owned draft."""
    try:
        summary = get_draft_store().rename(user_id, draft_id, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if summary is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    return summary


@router.delete("/drafts/{draft_id}")
def delete_draft(
    draft_id: str,
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict[str, str]:
    """Delete an owned draft."""
    if not get_draft_store().delete(user_id, draft_id):
        raise HTTPException(status_code=404, detail="Draft not found")
    return {"draft_id": draft_id, "status": "deleted"}
```

Keep all existing conversation/agent handlers unchanged.

**Monkeypatch note:** tests patch `routes.get_draft_store`. The handlers must call the **module-global** `get_draft_store` (imported name in `agent_studio.py`), so either:

```python
from agent_team_studio.agent_studio import drafts_runtime
# then drafts_runtime.get_draft_store()
```

and monkeypatch `routes.drafts_runtime.get_draft_store`, **or** keep `from … import get_draft_store` and monkeypatch `routes.get_draft_store` as the fixture does. Prefer the fixture’s approach: `from … import get_draft_store` and call `get_draft_store()` in handlers.

- [ ] **Step 4: Run route tests**

```bash
cd backend && python -m pytest unified_api/tests/test_agent_studio_drafts_routes.py -v
```

Expected: all PASS

Also confirm existing Studio routes still pass:

```bash
cd backend && python -m pytest unified_api/tests/test_agent_studio_routes.py -v --tb=short
```

- [ ] **Step 5: Lint**

```bash
cd backend && ruff check unified_api/routes/agent_studio.py \
  agents/agent_team_studio/agent_studio/models.py \
  unified_api/tests/test_agent_studio_drafts_routes.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py
ruff format unified_api/routes/agent_studio.py \
  agents/agent_team_studio/agent_studio/models.py \
  unified_api/tests/test_agent_studio_drafts_routes.py \
  agents/agent_team_studio/agent_studio/tests/test_drafts_request_models.py
```

- [ ] **Step 6: Commit**

```bash
git add \
  backend/unified_api/routes/agent_studio.py \
  backend/unified_api/tests/test_agent_studio_drafts_routes.py
git commit -m "$(cat <<'EOF'
Add user-scoped Agent Studio drafts HTTP routes.

Expose create/PUT/list/get/rename/delete over get_draft_store with a
pluggable user-id dependency and hermetic tenancy tests.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `get_current_user_id` default `DEFAULT_USER_ID` | Task 2 |
| POST create-only | Task 2 |
| PUT full update | Task 2 |
| GET list summaries + pagination | Task 2 |
| GET full draft | Task 2 |
| PATCH rename | Task 2 |
| DELETE with status body | Task 2 |
| Opaque SaveDraftRequest / RenameDraftRequest | Task 1 |
| 400 / 404 mapping | Task 2 |
| Tenancy tests via dependency override | Task 2 |
| Tag `agent-studio`, no main.py change | Task 2 |
