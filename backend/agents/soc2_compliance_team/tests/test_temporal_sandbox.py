"""Determinism / sandbox-safety guard for ``Soc2AuditWorkflow``.

The monkeypatched-``execute_activity`` tests in ``test_temporal.py`` prove the
orchestration order but do NOT install the temporalio workflow sandbox, so they
would miss a determinism regression — workflow code touching a sandbox-restricted
callable (``datetime.now``/``time.time``/``uuid4``/``os.getenv`` …) that raises
``RestrictedWorkflowAccessError`` at runtime under a real worker.

A full server-backed ``WorkflowEnvironment`` needs to download a test-server
binary and is unavailable in the offline CI network. Instead this guard mirrors
``investment_team/tests/test_strategy_lab_temporal_sandbox.py``: a **static source
scan** of the ``run`` body for the restricted names (the robust check for the
callables asyncio itself also needs, which can't be patched without breaking the
harness), plus a **runtime** check that patches ``uuid`` (never used by the event
loop) to raise and drives ``run`` end-to-end with activities mocked.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid as _uuid
from typing import Any, Dict
from unittest import mock

from soc2_compliance_team.temporal import workflows as wmod


class _SandboxViolation(RuntimeError):
    """Stand-in for ``RestrictedWorkflowAccessError`` raised by a patched call."""


def _boom(name: str):
    def _raise(*_a: Any, **_kw: Any) -> Dict[str, Any]:
        raise _SandboxViolation(
            f"workflow run path called sandbox-restricted {name!r} — this would raise "
            f"RestrictedWorkflowAccessError under the real temporalio sandbox"
        )

    return _raise


def _fake_execute_handlers():
    async def _fake_exec(fn, *, args, **_kw):  # noqa: ANN001
        name = fn.__name__
        if name == "load_repo_activity":
            return args[1]  # resolved repo path (a string)
        if name == "audit_criterion_activity":
            return {"category": args[1], "summary": "x", "findings": [], "compliant": True}
        if name == "write_report_activity":
            return {"status": "completed", "has_findings": False}
        return None

    return _fake_exec


def test_workflow_run_source_has_no_restricted_names() -> None:
    """The ``run`` body must not reference callables the temporalio sandbox blocks
    at workflow runtime — everything time/uuid/env-dependent lives in activities."""
    src = inspect.getsource(wmod.Soc2AuditWorkflow.run)
    banned = (
        "datetime.now",
        "datetime.utcnow",
        ".utcnow(",
        "date.today",
        "time.time(",
        "time.monotonic(",
        "uuid1(",
        "uuid4(",
        "os.getenv",
        "os.environ",
        "random.",
    )
    for name in banned:
        assert name not in src, f"workflow run path references sandbox-restricted {name!r}"


def test_workflow_run_touches_no_uuid() -> None:
    """Driving ``run`` end-to-end must not call ``uuid`` (a restricted callable),
    proving the workflow's own code is uuid-free."""
    handlers = _fake_execute_handlers()
    loop = asyncio.new_event_loop()
    try:
        with (
            mock.patch("temporalio.workflow.execute_activity", handlers),
            mock.patch.object(_uuid, "uuid4", _boom("uuid.uuid4")),
            mock.patch.object(_uuid, "uuid1", _boom("uuid.uuid1")),
        ):
            result = loop.run_until_complete(wmod.Soc2AuditWorkflow().run("job-1", "/repo/path"))
    finally:
        loop.close()
    assert result == {"status": "completed", "has_findings": False}


def test_sandbox_guard_trips() -> None:
    """Guard-the-guard: a coroutine that DOES call a restricted callable must be
    caught, proving the harness would flag a real regression."""

    async def _offending() -> Any:
        return _uuid.uuid4()

    loop = asyncio.new_event_loop()
    try:
        with mock.patch.object(_uuid, "uuid4", _boom("uuid.uuid4")):
            raised = False
            try:
                loop.run_until_complete(_offending())
            except _SandboxViolation:
                raised = True
        assert raised, "restriction harness failed to trip on uuid.uuid4()"
    finally:
        loop.close()
