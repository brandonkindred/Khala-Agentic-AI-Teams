# Agent Provisioning Single Temporal Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse Agent Provisioning onto one Temporal workflow (`AgentProvisioningWorkflow`) with renamed non-`_v2` activities, and delete all V1 / thread-fallback / drain capability.

**Architecture:** Promote today’s V2 workflow body to Temporal type `AgentProvisioningWorkflow`; rename `*_activity_v2` Python symbols; delete V1 workflow + `run_provisioning_activity`; make provision/resume/restart/deprovision HTTP routes Temporal-only (503 otherwise); remove `PROVISION_THREAD_FALLBACK` everywhere including sandbox dispatch gating.

**Tech Stack:** Python 3.10+, Temporal (`temporalio`), FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-07-15-agent-provisioning-single-workflow-design.md`

## Global Constraints

- **No backward compatibility. No drain period. No dual registration.** Never keep V1/V2 types registered together; no aliases, shims, or staged drain workers.
- No GitHub issue numbers in code, comments, commits, or docs (PR body only).
- DbC docstrings on every new/changed public helper (`Preconditions` / `Postconditions`).
- Temporal activity *string* names stay `agent_provisioning_*` (already non-versioned); only Python symbols drop `_v2`.
- Sandbox workflows on `SANDBOX_TASK_QUEUE` stay; only their fallback env gate changes.
- Hard cutover: one change set deletes legacy; unfinished jobs use `/resume`/`/restart`, not Temporal history drain.

## File map

| Path | Responsibility |
|---|---|
| `temporal/workflows.py` | Single `AgentProvisioningWorkflow`; delete V1 class |
| `temporal/activities.py` | Renamed phase activities; delete `run_provisioning_activity` |
| `temporal/__init__.py` | `WORKFLOWS`/`ACTIVITIES`/`__all__` without V1/V2 dual list |
| `temporal/start_workflow.py` | Start `AgentProvisioningWorkflow.run` |
| `temporal/client.py` | Drop `provision_thread_fallback_enabled` |
| `temporal/sandbox_dispatch.py` | `sandbox_temporal_enabled` → `is_temporal_enabled()` only |
| `api/main.py` | Temporal-required dispatch; delete thread runner / fallback / queue saturation |
| `README.md`, `sandbox/README.md` | Remove V2/legacy/fallback docs |
| Tests under `tests/` | Rewrite assertions; delete drain/fallback success cases |

---

### Task 1: Single workflow type (rename V2, delete V1)

**Files:**
- Modify: `backend/agents/agent_provisioning_team/temporal/workflows.py`
- Modify: `backend/agents/agent_provisioning_team/temporal/start_workflow.py`
- Modify: `backend/agents/agent_provisioning_team/temporal/__init__.py` (WORKFLOWS / `__all__` only in this task if activities still named `_v2` — update workflow exports now; activity symbol renames in Task 2)
- Modify: `backend/agents/agent_provisioning_team/tests/test_workflows_unit.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_temporal_integration.py` (registry assertions for workflow classes)
- Modify: `backend/agents/agent_provisioning_team/tests/test_temporal_unit.py` (start_workflow target)

**Interfaces:**
- Produces:
  - `@workflow.defn(name="AgentProvisioningWorkflow") class AgentProvisioningWorkflow`
  - `async def run(self, job_id: str, agent_id: str, manifest_path: str, skip_phases: list[str] | None = None, prior_results: dict[str, Any] | None = None) -> None`
  - `start_provisioning_workflow` starts `AgentProvisioningWorkflow.run`
- Consumes: existing `*_activity_v2` / `provision_tool_activity` symbols until Task 2

- [ ] **Step 1: Rewrite failing workflow unit tests**

In `test_workflows_unit.py`:
- Replace every `AgentProvisioningWorkflowV2` with `AgentProvisioningWorkflow`.
- Delete `test_workflow_v1_invokes_single_activity` entirely.
- Rename test functions from `test_workflow_v2_*` → `test_workflow_*` (drop `v2`).
- Keep stub maps keyed by activity **function names** as they exist today (`setup_activity_v2`, etc.) until Task 2.

- [ ] **Step 2: Run FAIL (name still V2)**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_workflows_unit.py -v --tb=line
```

Expected: FAIL — `AgentProvisioningWorkflowV2` AttributeError or import/class mismatch after test rename (or assert failure if class still named V2).

- [ ] **Step 3: Implement workflow file**

Replace `workflows.py` module docstring and classes:

```python
"""Temporal workflows for the Agent Provisioning team.

``AgentProvisioningWorkflow`` decomposes provisioning into per-phase activities
and fans out tool provisioning in parallel via ``asyncio.gather``.
``AgentDeprovisioningWorkflow`` tears down one agent as a single activity.
"""
# ... keep constants/retry policies ...

@workflow.defn(name="AgentProvisioningWorkflow")
class AgentProvisioningWorkflow:
    """Per-phase activities with parallel per-tool fan-out."""

    @workflow.run
    async def run(
        self,
        job_id: str,
        agent_id: str,
        manifest_path: str,
        skip_phases: list[str] | None = None,
        prior_results: dict[str, Any] | None = None,
    ) -> None:
        # SAME BODY as today's AgentProvisioningWorkflowV2.run
        ...

# DELETE entire AgentProvisioningWorkflow V1 class (single-activity)

@workflow.defn(name="AgentDeprovisioningWorkflow")
class AgentDeprovisioningWorkflow:
    """Deprovision one agent's resources as a single durable activity.

    The teardown counterpart to :class:`AgentProvisioningWorkflow`.
    ...
    """
```

- [ ] **Step 4: Point starter + registry at the single class**

`start_workflow.py`:

```python
from agent_provisioning_team.temporal.workflows import (
    AgentDeprovisioningWorkflow,
    AgentProvisioningWorkflow,
)

def start_provisioning_workflow(...):
    """Start ``AgentProvisioningWorkflow`` for the given job."""
    ...
    client.start_workflow(
        AgentProvisioningWorkflow.run,
        args=[...],
        ...
    )
    logger.info("Started AgentProvisioningWorkflow id=%s", workflow_id)
```

`__init__.py` WORKFLOWS (activities list still has V1 activity until Task 2 — remove only the V1 *workflow* now):

```python
from agent_provisioning_team.temporal.workflows import (
    AgentDeprovisioningWorkflow,
    AgentProvisioningWorkflow,
)

WORKFLOWS = [
    AgentProvisioningWorkflow,
    AgentDeprovisioningWorkflow,
]
```

Update `__all__` accordingly (drop `AgentProvisioningWorkflowV2`).

- [ ] **Step 5: Fix integration/unit assertions**

- `test_temporal_integration.py`: `assert AgentProvisioningWorkflow in t.WORKFLOWS`; assert V2 class is absent (`hasattr` / import must fail or not in list).
- `test_temporal_unit.py`: starter must pass `AgentProvisioningWorkflow.run` (adjust any string/log assertions).

- [ ] **Step 6: Run PASS**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_workflows_unit.py \
  agents/agent_provisioning_team/tests/test_temporal_unit.py::test_start_provisioning_workflow_passes_args \
  agents/agent_provisioning_team/tests/test_temporal_integration.py::test_provision_routes_to_v2_when_temporal_enabled \
  -v --tb=short
```

Rename integration test `test_provision_routes_to_v2_when_temporal_enabled` → `test_provision_routes_to_temporal_when_enabled` in this step (or Task 5 if it still mentions fallback).

Expected: PASS for updated tests (legacy fallback tests may still exist and are deleted in Task 4/5).

- [ ] **Step 7: Commit**

```bash
git add backend/agents/agent_provisioning_team/temporal/workflows.py \
  backend/agents/agent_provisioning_team/temporal/start_workflow.py \
  backend/agents/agent_provisioning_team/temporal/__init__.py \
  backend/agents/agent_provisioning_team/tests/test_workflows_unit.py \
  backend/agents/agent_provisioning_team/tests/test_temporal_integration.py \
  backend/agents/agent_provisioning_team/tests/test_temporal_unit.py
git commit -m "$(cat <<'EOF'
Collapse Agent Provisioning onto a single Temporal workflow type.

EOF
)"
```

---

### Task 2: Rename phase activities; delete V1 activity

**Files:**
- Modify: `backend/agents/agent_provisioning_team/temporal/activities.py`
- Modify: `backend/agents/agent_provisioning_team/temporal/workflows.py` (call sites)
- Modify: `backend/agents/agent_provisioning_team/temporal/__init__.py` (`ACTIVITIES` / imports)
- Modify: `backend/agents/agent_provisioning_team/tests/test_workflows_unit.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_temporal_unit.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_temporal_integration.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_workflows_unit.py` setup activity tests (top of file)

**Interfaces:**
- Produces function symbols:
  - `setup_activity`, `credentials_activity`, `audit_activity`, `documentation_activity`, `deliver_activity`, `compensate_activity`
  - Temporal `@activity.defn(name=...)` strings **unchanged** (`agent_provisioning_setup`, …)
- Deletes: `run_provisioning_activity` / name `run_agent_provisioning`

- [ ] **Step 1: Update tests to new symbols (will FAIL)**

Replace all `*_activity_v2` references with `*_activity` in the three test modules and workflow stub maps, e.g.:

```python
"setup_activity": {"success": True, "environment": {"workspace_path": "/w"}},
"credentials_activity": {...},
# ...
```

Delete `test_v1_activity_delegates_to_run_provisioning_background` from `test_temporal_unit.py`.

Rename test functions `test_*_activity_v2_*` → `test_*_activity_*`.

- [ ] **Step 2: Run FAIL**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_workflows_unit.py \
  agents/agent_provisioning_team/tests/test_temporal_unit.py \
  agents/agent_provisioning_team/tests/test_temporal_integration.py \
  -v --tb=line -q
```

Expected: FAIL — `ImportError` / missing attributes for renamed symbols.

- [ ] **Step 3: Rename in `activities.py`; delete V1 block**

Module docstring becomes single-surface (no “v1 kept for drain”). Delete the entire `# v1 — single-shot` section (`run_provisioning_activity`).

Rename defs:

```python
@activity.defn(name="agent_provisioning_setup")
def setup_activity(...): ...

@activity.defn(name="agent_provisioning_credentials")
def credentials_activity(...): ...

# same pattern for audit, documentation, deliver, compensate
```

Keep `provision_tool_activity` / `deprovision_activity` names.

- [ ] **Step 4: Update workflow call sites + `__init__.py` ACTIVITIES**

`workflows.py`: `_activities.setup_activity`, etc.

```python
ACTIVITIES = [
    setup_activity,
    credentials_activity,
    provision_tool_activity,
    audit_activity,
    documentation_activity,
    deliver_activity,
    compensate_activity,
    deprovision_activity,
]
```

No `run_provisioning_activity`.

- [ ] **Step 5: Run PASS**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_workflows_unit.py \
  agents/agent_provisioning_team/tests/test_temporal_unit.py \
  agents/agent_provisioning_team/tests/test_temporal_integration.py \
  -v --tb=short
```

Expected: PASS (except any remaining fallback API cases still expecting old behavior — leave those for Task 4/5).

- [ ] **Step 6: Commit**

```bash
git add backend/agents/agent_provisioning_team/temporal/activities.py \
  backend/agents/agent_provisioning_team/temporal/workflows.py \
  backend/agents/agent_provisioning_team/temporal/__init__.py \
  backend/agents/agent_provisioning_team/tests/test_workflows_unit.py \
  backend/agents/agent_provisioning_team/tests/test_temporal_unit.py \
  backend/agents/agent_provisioning_team/tests/test_temporal_integration.py
git commit -m "$(cat <<'EOF'
Rename provisioning phase activities and drop the single-shot activity.

EOF
)"
```

---

### Task 3: Drop `PROVISION_THREAD_FALLBACK` from client + sandbox dispatch

**Files:**
- Modify: `backend/agents/agent_provisioning_team/temporal/client.py`
- Modify: `backend/agents/agent_provisioning_team/temporal/sandbox_dispatch.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_sandbox_temporal.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_temporal_unit.py` (if any fallback client tests)

**Interfaces:**
- Deletes: `provision_thread_fallback_enabled()`
- Produces:
  - `sandbox_temporal_enabled() -> bool` with body equivalent to `return is_temporal_enabled()`

- [ ] **Step 1: Rewrite sandbox fallback tests to assert new contract**

Delete tests that set `PROVISION_THREAD_FALLBACK=1` and expect `sandbox_temporal_enabled() is False` while Temporal is enabled.

Replace with:

```python
def test_sandbox_temporal_enabled_follows_is_temporal_enabled(monkeypatch):
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")
    # ensure no leftover fallback env matters — var must be ignored/absent from code
    monkeypatch.setenv("PROVISION_THREAD_FALLBACK", "1")
    from agent_provisioning_team.temporal import sandbox_dispatch as sd
    assert sd.sandbox_temporal_enabled() is True

def test_sandbox_temporal_disabled_without_address(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from agent_provisioning_team.temporal import sandbox_dispatch as sd
    assert sd.sandbox_temporal_enabled() is False
```

Delete tests that asserted `sandbox_temporal_enabled` delegates to `provision_thread_fallback_enabled`.

- [ ] **Step 2: Run FAIL**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_sandbox_temporal.py -v --tb=line
```

Expected: FAIL — fallback still disables Temporal when env set.

- [ ] **Step 3: Implement**

`client.py`: delete `provision_thread_fallback_enabled` and any `os` import used only for it.

`sandbox_dispatch.py`:

```python
def sandbox_temporal_enabled() -> bool:
    """True when sandbox lifecycle ops should dispatch to Temporal.

    Postconditions:
        * Returns ``is_temporal_enabled()`` — never raises.
    """
    from agent_provisioning_team.temporal.client import is_temporal_enabled

    return is_temporal_enabled()
```

Update module/doc comments that mentioned the escape hatch.

- [ ] **Step 4: Run PASS**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_sandbox_temporal.py -v --tb=short
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/agent_provisioning_team/temporal/client.py \
  backend/agents/agent_provisioning_team/temporal/sandbox_dispatch.py \
  backend/agents/agent_provisioning_team/tests/test_sandbox_temporal.py \
  backend/agents/agent_provisioning_team/tests/test_temporal_unit.py
git commit -m "$(cat <<'EOF'
Remove PROVISION_THREAD_FALLBACK from sandbox Temporal gating.

EOF
)"
```

---

### Task 4: API Temporal-only — provision / resume / restart / deprovision

**Files:**
- Modify: `backend/agents/agent_provisioning_team/api/main.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_api_unit.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_api.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_temporal_integration.py`
- Modify: `backend/agents/agent_provisioning_team/tests/test_deprovision_temporal.py`

**Interfaces:**
- Produces:
  - `_require_provision_starter() -> Callable` — raises `HTTPException(503)` if Temporal unavailable
  - `_require_deprovision_runner() -> Callable` — same
- Deletes from `api/main.py`:
  - `_provision_thread_fallback`, `_temporal_starter` dual-path, `_deprovision_starter` dual-path
  - `_run_provisioning_background`, `_submit_provisioning_job`
  - `_ensure_executor`, `_queue_depth`, `_reject_if_saturated`, `_inflight*`, `PROVISION_MAX_WORKERS`, `PROVISION_MAX_QUEUE_DEPTH`
  - ThreadPoolExecutor lifespan wiring used only for provision submits
- Keeps: `_graceful_shutdown` compensate-from-job-store + `mark_all_running_jobs_failed` (no executor drain); lifespan clears `_shutdown_event` if still needed for other reasons — if `_shutdown_event` becomes unused after deleting thread runner, delete it too.

- [ ] **Step 1: Write failing API tests for 503 / Temporal-required**

Delete (do not keep as xfail):
- `test_provision_uses_thread_path_when_no_temporal`
- `test_provision_thread_fallback_*` / `test_provision_falls_back_to_thread_path_when_flag_set`
- `test_resume_job_thread_path` / `test_restart_job_thread_path`
- All `test_run_provisioning_background_*`
- `test_ensure_executor_*` / `test_reject_if_saturated_*` / inflight executor tests that only exist for the thread runner
- `test_provision_rejects_when_queue_saturated` (429 backpressure)
- Deprovision tests that expect `_deprovision_starter() is None` → in-process success

Add:

```python
def test_provision_returns_503_when_temporal_disabled(monkeypatch):
    monkeypatch.setattr(
        "agent_provisioning_team.temporal.client.is_temporal_enabled",
        lambda: False,
    )
    # patch starter path via whatever helper name Task 4 introduces
    from fastapi.testclient import TestClient
    from agent_provisioning_team.api import main as api_main
    client = TestClient(api_main.app)
    r = client.post("/provision", json={"agent_id": "a1"})
    assert r.status_code == 503
    assert "Temporal" in r.json()["detail"]


def test_provision_starts_workflow_when_temporal_enabled(monkeypatch):
    calls = []
    def fake_start(job_id, agent_id, manifest_path, skip_phases=None, prior_results=None):
        calls.append((job_id, agent_id, manifest_path, skip_phases, prior_results))
    monkeypatch.setattr(
        "agent_provisioning_team.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "agent_provisioning_team.temporal.start_workflow.start_provisioning_workflow",
        fake_start,
    )
    # If helper imports lazily, also patch api_main._require_provision_starter return
    ...
    r = client.post("/provision", json={"agent_id": "a1", "manifest_path": "default.yaml"})
    assert r.status_code == 200
    assert calls and calls[0][1] == "a1"
```

Mirror for resume/restart (503 when disabled; starter args include `skip_phases`/`prior_results` on resume).

Deprovision:

```python
def test_deprovision_returns_503_when_temporal_disabled(...):
    ...
    r = client.delete("/environments/agent-1")
    assert r.status_code == 503
```

When Temporal enabled but `run_deprovision_workflow` raises: keep existing `success=False` response shape (not 503) — only *unconfigured* Temporal is 503.

- [ ] **Step 2: Run FAIL**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_api_unit.py \
  agents/agent_provisioning_team/tests/test_api.py \
  agents/agent_provisioning_team/tests/test_temporal_integration.py \
  agents/agent_provisioning_team/tests/test_deprovision_temporal.py \
  -v --tb=line -q
```

Expected: FAIL — routes still thread-fallback / 200 without Temporal.

- [ ] **Step 3: Implement helpers + route bodies**

```python
_TEMPORAL_REQUIRED = "Temporal is required for agent provisioning (set TEMPORAL_ADDRESS)"

def _require_provision_starter():
    """Return ``start_provisioning_workflow`` or raise HTTP 503.

    Preconditions:
        * None (reads process env / Temporal client state).
    Postconditions:
        * Returns the starter callable, or raises ``HTTPException`` with status 503.
    """
    try:
        from agent_provisioning_team.temporal.client import is_temporal_enabled
        from agent_provisioning_team.temporal.start_workflow import start_provisioning_workflow
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED) from exc
    if not is_temporal_enabled():
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED)
    return start_provisioning_workflow


def _require_deprovision_runner():
    """Return ``run_deprovision_workflow`` or raise HTTP 503."""
    try:
        from agent_provisioning_team.temporal.client import is_temporal_enabled
        from agent_provisioning_team.temporal.start_workflow import run_deprovision_workflow
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED) from exc
    if not is_temporal_enabled():
        raise HTTPException(status_code=503, detail=_TEMPORAL_REQUIRED)
    return run_deprovision_workflow
```

`start_provisioning`:

```python
def start_provisioning(request: ProvisionRequest) -> ProvisionJobResponse:
    starter = _require_provision_starter()  # 503 before create_job
    job_id = str(uuid.uuid4())
    create_job(job_id=job_id, agent_id=request.agent_id, manifest_path=request.manifest_path)
    try:
        starter(job_id, request.agent_id, request.manifest_path, skip_phases=None, prior_results=None)
    except HTTPException:
        raise
    except Exception as exc:
        mark_job_failed(job_id, error=str(exc))
        raise HTTPException(status_code=503, detail=f"Failed to start Temporal workflow: {exc}") from exc
    return ProvisionJobResponse(
        job_id=job_id,
        status=JOB_STATUS_RUNNING,
        message="Provisioning started (Temporal). Poll GET /provision/status/{job_id} for progress.",
    )
```

Same pattern for resume/restart (call `_require_provision_starter()`; no `_submit_provisioning_job`).

`deprovision_agent`:

```python
def deprovision_agent(agent_id: str, force: bool = Query(False)) -> DeprovisionResponse:
    runner = _require_deprovision_runner()  # 503 if Temporal off
    try:
        return DeprovisionResponse.model_validate(runner(agent_id, force))
    except Exception as exc:
        logger.exception("Durable deprovision failed for agent=%s", agent_id)
        return DeprovisionResponse(
            agent_id=agent_id,
            success=False,
            details={},
            error=f"Deprovision workflow failed: {exc}",
        )
```

Delete all thread-executor provision machinery listed above. Simplify lifespan:

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        await _graceful_shutdown()
```

Rewrite `_graceful_shutdown` without executor drain — keep list running jobs → `_safe_compensate` → `mark_all_running_jobs_failed`.

Remove unused imports (`ThreadPoolExecutor`, `Future`, etc.) once nothing references them.

- [ ] **Step 4: Run PASS**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/test_api_unit.py \
  agents/agent_provisioning_team/tests/test_api.py \
  agents/agent_provisioning_team/tests/test_temporal_integration.py \
  agents/agent_provisioning_team/tests/test_deprovision_temporal.py \
  -v --tb=short
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/agent_provisioning_team/api/main.py \
  backend/agents/agent_provisioning_team/tests/test_api_unit.py \
  backend/agents/agent_provisioning_team/tests/test_api.py \
  backend/agents/agent_provisioning_team/tests/test_temporal_integration.py \
  backend/agents/agent_provisioning_team/tests/test_deprovision_temporal.py
git commit -m "$(cat <<'EOF'
Require Temporal for provision and deprovision HTTP entrypoints.

EOF
)"
```

---

### Task 5: Docs + grep verification + full team test suite

**Files:**
- Modify: `backend/agents/agent_provisioning_team/README.md`
- Modify: `backend/agents/agent_provisioning_team/sandbox/README.md`
- Modify: `docs/superpowers/specs/2026-07-15-agent-provisioning-single-workflow-design.md` (Status → Approved)
- Sweep: any remaining team references via grep

- [ ] **Step 1: Update README coverage table**

Replace Temporal section rows with a single provision line:

| Operation | Entry point | Workflow | Activities |
|---|---|---|---|
| Provision | `POST /provision` | `AgentProvisioningWorkflow` | `setup` / `credentials` / per-tool `provision_tool` (parallel) / `audit` / `documentation` / `deliver` / `compensate` |
| Resume / restart | `POST /provision/job/{id}/resume`·`/restart` | `AgentProvisioningWorkflow` (`skip_phases` + `prior_results`) | same |
| Deprovision | `DELETE /environments/{agent_id}` | `AgentDeprovisioningWorkflow` | `deprovision_activity` |

Delete “Provision (legacy)” row and all `PROVISION_THREAD_FALLBACK` / “escape hatch” paragraphs. State clearly: Temporal is required for provision and deprovision.

`sandbox/README.md`: remove fallback env sentence; gate description = `TEMPORAL_ADDRESS` / `is_temporal_enabled()` only.

- [ ] **Step 2: Grep must be clean**

```bash
cd backend/agents/agent_provisioning_team && \
  ! rg -n 'AgentProvisioningWorkflowV2|run_provisioning_activity|_activity_v2|PROVISION_THREAD_FALLBACK|provision_thread_fallback|_run_provisioning_background|_temporal_starter|_deprovision_starter' .
```

Expected: no matches (exit 0 because of `!` + empty rg). If CHANGELOG or historical notes match, remove live docs hits; do not reintroduce drain wording.

Also repo-wide quick check:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams && \
  rg -n 'PROVISION_THREAD_FALLBACK|AgentProvisioningWorkflowV2|run_provisioning_activity' \
    backend docs || true
```

- [ ] **Step 3: Full team pytest**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/agent_provisioning_team/tests/ -v --tb=short
```

Expected: PASS (fix any residual rename fallout).

- [ ] **Step 4: Mark design Approved; commit**

Set spec `**Status:** Approved 2026-07-15`.

```bash
git add backend/agents/agent_provisioning_team/README.md \
  backend/agents/agent_provisioning_team/sandbox/README.md \
  docs/superpowers/specs/2026-07-15-agent-provisioning-single-workflow-design.md
git commit -m "$(cat <<'EOF'
Document single-workflow Temporal-only agent provisioning cutover.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Single `AgentProvisioningWorkflow` Temporal type | Task 1 |
| Delete V1 workflow class | Task 1 |
| Rename `*_activity_v2` Python symbols | Task 2 |
| Keep Temporal activity string names; delete `run_agent_provisioning` | Task 2 |
| No drain / dual registration | Tasks 1–2 (registry) |
| Remove `PROVISION_THREAD_FALLBACK` / client helper | Task 3 |
| Sandbox gates on `is_temporal_enabled` only | Task 3 |
| API provision/resume/restart/deprovision Temporal-only + 503 | Task 4 |
| Delete thread runner / queue saturation / `_run_provisioning_background` | Task 4 |
| Docs + grep success criteria | Task 5 |
| Sandbox workflows unchanged structurally | Tasks 1–5 (no sandbox workflow edits) |

## Plan self-review

1. **Spec coverage:** All hard-rule / goals rows mapped above; no drain left as optional.
2. **Placeholders:** None — helpers, route shapes, deletes, and commands are concrete. Minor `...` only where existing V2 body is copied verbatim unchanged.
3. **Type consistency:** Surviving workflow `AgentProvisioningWorkflow.run(...)`; activities without `_v2`; starters named `_require_provision_starter` / `_require_deprovision_runner` consistently in Task 4.
