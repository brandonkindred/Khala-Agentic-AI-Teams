# SE Startup Fail-Fast Temporal Assertion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Abort SE ASGI startup when Temporal is disabled or unreachable, instead of log-and-continue, so the process never serves traffic without a working Temporal path.

**Architecture:** Extract async `_assert_temporal_ready()` that requires `is_temporal_enabled()` and `await connect_temporal_client()`. Make `_se_startup` async, await the assert first (no try/except), then keep existing log-and-continue blocks for telemetry, workers, and CodeEngineProvider.

**Tech Stack:** Python 3.10, `shared.temporal.client`, pytest + `pytest.mark.asyncio` / `asyncio.run` as used by the SE suite, monkeypatch.

**Spec:** `docs/superpowers/specs/2026-08-07-se-startup-fail-fast-design.md`

## Global Constraints

- Fail when `TEMPORAL_ADDRESS` is unset (`is_temporal_enabled()` is False) **or** when `connect_temporal_client()` raises.
- Probe via `await connect_temporal_client()` — real connect, not address-only.
- Do **not** change `shared.temporal.worker.start_team_worker` no-op-when-disabled behavior.
- Telemetry / CodeEngineProvider / worker-start try/except blocks stay log-and-continue after the assert.
- Default pytest must stay hermetic (mock Temporal; no live server).
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Design by Contract: document Preconditions/Postconditions on `_assert_temporal_ready` and update `_se_startup`'s docstring (it no longer "never aborts").
- Work exclusively in `.worktrees/4000-se-startup-fail-fast` on branch `feature/4000-se-startup-fail-fast`.
- Run tests from the worktree's `backend/` using the main-repo venv when needed:
  `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`
  with cwd = `.worktrees/4000-se-startup-fail-fast/backend`.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/api/lifecycle.py` | `_assert_temporal_ready` + async `_se_startup` |
| `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py` | Unit tests for assert + thin `_se_startup` ordering test |

No shared-infra changes.

---

### Task 1: Failing tests for `_assert_temporal_ready`

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py`
- Test: same file

**Interfaces:**
- Consumes: (none yet — tests will import symbols that do not exist)
- Produces: `test_assert_temporal_ready_raises_when_disabled`, `test_assert_temporal_ready_propagates_connect_failure`, `test_assert_temporal_ready_succeeds_when_connect_ok`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py`:

```python
"""SE ASGI startup must fail fast when Temporal is missing or unreachable."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from software_engineering_team.api import lifecycle


@pytest.mark.asyncio
async def test_assert_temporal_ready_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: False,
    )
    connect = AsyncMock()
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        connect,
    )

    with pytest.raises(RuntimeError, match="TEMPORAL_ADDRESS"):
        await lifecycle._assert_temporal_ready()

    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_assert_temporal_ready_propagates_connect_failure(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(side_effect=OSError("connection refused")),
    )

    with pytest.raises(OSError, match="connection refused"):
        await lifecycle._assert_temporal_ready()


@pytest.mark.asyncio
async def test_assert_temporal_ready_succeeds_when_connect_ok(monkeypatch):
    monkeypatch.setattr(
        "shared.temporal.client.is_temporal_enabled",
        lambda: True,
    )
    client = object()
    monkeypatch.setattr(
        "shared.temporal.client.connect_temporal_client",
        AsyncMock(return_value=client),
    )

    await lifecycle._assert_temporal_ready()
```

If the suite does not collect `@pytest.mark.asyncio` without a plugin, use this sync wrapper pattern instead for all three tests (check neighboring SE async tests first — prefer matching the local convention):

```python
import asyncio

def test_assert_temporal_ready_raises_when_disabled(monkeypatch):
    ...
    asyncio.run(lifecycle._assert_temporal_ready())  # inside pytest.raises
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/4000-se-startup-fail-fast/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_se_startup_fail_fast.py -v
```

Expected: FAIL with `AttributeError: module ... has no attribute '_assert_temporal_ready'` (or ImportError).

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py
git commit -m "$(cat <<'EOF'
Add failing tests for SE Temporal startup fail-fast assert.

EOF
)"
```

---

### Task 2: Implement `_assert_temporal_ready` and wire into `_se_startup`

**Files:**
- Modify: `backend/agents/software_engineering_team/api/lifecycle.py`
- Test: `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py`

**Interfaces:**
- Consumes: `shared.temporal.is_temporal_enabled`, `shared.temporal.connect_temporal_client`
- Produces: `async def _assert_temporal_ready() -> None`, `async def _se_startup() -> None`

- [ ] **Step 1: Implement the assert helper and make `_se_startup` async**

Replace the module docstring and `_se_startup` in `lifecycle.py` so the file begins:

```python
"""ASGI startup/shutdown hooks for the SE team app.

Passed to ``create_team_app`` by ``main``. Startup fails fast when Temporal is
disabled or unreachable; other individual steps remain log-and-continue so a
non-Temporal failure never leaks the Postgres pool.
"""

import logging

logger = logging.getLogger(__name__)


async def _assert_temporal_ready() -> None:
    """Require a live Temporal connection before SE starts serving.

    Preconditions:
        - None (reads Temporal env via ``is_temporal_enabled`` / connect helpers).

    Postconditions:
        - Raises ``RuntimeError`` when Temporal is disabled (no ``TEMPORAL_ADDRESS``).
        - Awaits ``connect_temporal_client`` when enabled; propagates connect errors.
        - Returns only after a successful connect probe.
    """
    from shared.temporal import connect_temporal_client, is_temporal_enabled

    if not is_temporal_enabled():
        raise RuntimeError(
            "SE requires TEMPORAL_ADDRESS; refusing to start without Temporal"
        )
    await connect_temporal_client()


async def _se_startup() -> None:  # pragma: no cover - integration-only ASGI startup hook
    """Register SE telemetry observers and start SE's + coding_team's Temporal workers.

    Runs after the factory has registered the SE Postgres schema. Fails fast
    when Temporal is disabled or unreachable (see ``_assert_temporal_ready``).
    Subsequent steps are log-and-continue so a single non-Temporal failure never
    leaks the Postgres pool the factory may have opened.

    Also installs the SE-backed ``CodeEngineProvider`` and starts coding_team's
    own Temporal worker (on its own task queue) — this is the in-process
    replacement for what the now-retired standalone ``coding-team-service``
    container and its ``coding_team_service`` composition root used to do.
    """
    await _assert_temporal_ready()
    try:
        from software_engineering_team.shared.cost_tracker import register_cost_observer
        from software_engineering_team.shared.trace_flusher import register_trace_flusher

        register_cost_observer()
        register_trace_flusher()
    except Exception as e:
        logger.warning("Could not register SE telemetry observers: %s", e)
    try:
        from software_engineering_team.temporal.worker import start_se_temporal_worker_thread

        start_se_temporal_worker_thread()
    except Exception as e:
        logger.warning("Could not start SE Temporal worker: %s", e)
    try:
        from software_engineering_team.coding_engine_provider import SECodeEngineProvider
        from software_engineering_team.engine_provider import set_engine_provider

        set_engine_provider(SECodeEngineProvider())
    except Exception as e:
        logger.warning("Could not install SE-backed CodeEngineProvider for coding_team: %s", e)
    try:
        from software_engineering_team.temporal.coding_team_worker import (
            start_coding_team_temporal_worker_thread,
        )

        start_coding_team_temporal_worker_thread()
    except Exception as e:
        logger.warning("Could not start coding_team Temporal worker: %s", e)
```

Leave `_se_shutdown` unchanged.

**Import note for tests:** Task 1 patches `shared.temporal.client.*`. The implementation imports from `shared.temporal` (re-export package). Either:

1. Patch where used: `monkeypatch.setattr(lifecycle, ...)` after importing into the helper via local imports — prefer patching `shared.temporal.is_temporal_enabled` and `shared.temporal.connect_temporal_client` in the tests to match the import path in the helper, **or**
2. Keep helper importing from `shared.temporal.client` so Task 1 patches match.

Choose option 2 if Task 1 tests already pass with those patch paths; otherwise update the three tests to patch `shared.temporal.is_temporal_enabled` / `shared.temporal.connect_temporal_client`.

- [ ] **Step 2: Run Task 1 tests — expect PASS**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/4000-se-startup-fail-fast/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_se_startup_fail_fast.py -v
```

Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/api/lifecycle.py \
  backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py
git commit -m "$(cat <<'EOF'
Fail fast in SE startup when Temporal is missing or unreachable.

EOF
)"
```

---

### Task 3: Thin `_se_startup` ordering test

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py`

**Interfaces:**
- Consumes: `lifecycle._se_startup`, `lifecycle._assert_temporal_ready`
- Produces: `test_se_startup_awaits_assert_before_workers`

- [ ] **Step 1: Write the failing ordering test**

Append to `test_se_startup_fail_fast.py`:

```python
@pytest.mark.asyncio
async def test_se_startup_awaits_assert_before_workers(monkeypatch):
    """Fail-fast assert must run before either Temporal worker start."""
    calls: list[str] = []

    async def _assert() -> None:
        calls.append("assert")

    monkeypatch.setattr(lifecycle, "_assert_temporal_ready", _assert)

    def _se_worker() -> bool:
        calls.append("se_worker")
        return True

    def _ct_worker() -> bool:
        calls.append("ct_worker")
        return True

    monkeypatch.setattr(
        "software_engineering_team.temporal.worker.start_se_temporal_worker_thread",
        _se_worker,
    )
    monkeypatch.setattr(
        "software_engineering_team.temporal.coding_team_worker.start_coding_team_temporal_worker_thread",
        _ct_worker,
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.cost_tracker.register_cost_observer",
        lambda: calls.append("telemetry"),
    )
    monkeypatch.setattr(
        "software_engineering_team.shared.trace_flusher.register_trace_flusher",
        lambda: None,
    )
    monkeypatch.setattr(
        "software_engineering_team.coding_engine_provider.SECodeEngineProvider",
        lambda: object(),
    )
    monkeypatch.setattr(
        "software_engineering_team.engine_provider.set_engine_provider",
        lambda _p: calls.append("engine"),
    )

    await lifecycle._se_startup()

    assert calls[0] == "assert"
    assert "se_worker" in calls
    assert "ct_worker" in calls
```

- [ ] **Step 2: Run the new test**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/4000-se-startup-fail-fast/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_se_startup_fail_fast.py::test_se_startup_awaits_assert_before_workers -v
```

Expected: PASS (implementation already awaits assert first). If it fails because imports happen inside try blocks and patches miss, patch via `sys.modules` or import the modules first then `monkeypatch.setattr(module, "start_...", ...)`. Adjust until green without weakening the assert-first requirement.

- [ ] **Step 3: Run the full new test file**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/4000-se-startup-fail-fast/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_se_startup_fail_fast.py -v
```

Expected: all tests in the file PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_se_startup_fail_fast.py
git commit -m "$(cat <<'EOF'
Assert SE startup runs Temporal fail-fast check before workers.

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| `_assert_temporal_ready` when disabled → `RuntimeError` | Task 1 + 2 |
| Connect failure propagates | Task 1 + 2 |
| Successful connect returns | Task 1 + 2 |
| `_se_startup` awaits assert before workers | Task 2 + 3 |
| Other steps stay log-and-continue | Task 2 (unchanged blocks) |
| No `start_team_worker` change | Global constraint / no file touch |
| Hermetic pytest (mocked Temporal) | All tasks |
| DbC docstrings | Task 2 |
