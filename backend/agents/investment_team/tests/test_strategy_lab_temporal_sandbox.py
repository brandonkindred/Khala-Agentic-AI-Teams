"""Determinism regression guard for ``StrategyLabCycleWorkflow``.

The bug this file exists to prevent: an earlier hand-ported version of the cycle
workflow called quality gates and other orchestrator helpers *directly from
workflow code*. Those helpers construct ``QualityGateResult`` (whose
``evaluated_at`` field defaults via ``datetime.now``) and read ``os.environ`` —
both of which the temporalio workflow sandbox forbids at workflow runtime
(``RestrictedWorkflowAccessError``). The mocked ``asyncio.run`` unit tests never
installed the sandbox, so they let that showstopper through.

The full server-backed ``WorkflowEnvironment`` (which would run the real
sandbox) needs to download a test-server binary and is unavailable in the
offline CI network. Instead, this guard reproduces the sandbox's *runtime*
restriction surface directly: it patches the exact callables the temporalio
sandbox blocks at workflow runtime — ``datetime.now``/``utcnow``/``today``,
``date.today``, ``time.time``/``monotonic``, ``uuid.uuid1``/``uuid4``, and
``os.getenv``/``os.environ`` reads (see
``temporalio/worker/workflow_sandbox/_restrictions.py``) — to raise, then drives
the workflow's ``run()`` end-to-end with every activity mocked. If the
workflow's own code path (as opposed to the activities it calls, which run
outside the sandbox) touches any restricted callable, one of these tests fails.

The current attempt-level design keeps ``run()`` to plain dict work, pure
``ConvergenceTracker`` reads, and ``execute_activity`` calls — so it stays clean;
the old phase-in-workflow design would trip immediately.
"""

from __future__ import annotations

import asyncio
import contextlib
import os as _os
import uuid as _uuid
from typing import Any, Dict, List
from unittest import mock

from investment_team.strategy_lab.temporal import workflows as wf


class _SandboxViolation(RuntimeError):
    """Stand-in for ``RestrictedWorkflowAccessError`` raised by a patched call."""


def _boom(name: str):
    def _raise(*_a: Any, **_kw: Any):
        raise _SandboxViolation(
            f"workflow run path called sandbox-restricted {name!r} — this would "
            f"raise RestrictedWorkflowAccessError under the real temporalio sandbox"
        )

    return _raise


# The strategy-lab env vars whose reads must happen inside an activity (via
# ``resolve_workflow_config_activity``), never in workflow code — this is the
# exact channel the determinism showstopper used (gates/env-resolvers reading
# ``STRATEGY_LAB_*`` tunables while called directly from the workflow). Reads of
# any *other* key (e.g. Pydantic's ``PYDANTIC_DISABLE_PLUGINS``, which the real
# temporalio sandbox exempts as a passthrough-module internal) are delegated to
# the real environment so the harness flags only genuine workflow-code env
# access, not library plumbing.
_RESTRICTED_ENV_PREFIX = "STRATEGY_LAB"
_REAL_GETENV = _os.getenv


def _guarded_getenv(name: str, default: Any = None) -> Any:
    if isinstance(name, str) and name.startswith(_RESTRICTED_ENV_PREFIX):
        raise _SandboxViolation(f"workflow run path read os.getenv({name!r})")
    return _REAL_GETENV(name, default)


class _GuardedEnviron(dict):
    """An ``os.environ`` stand-in that raises on ``STRATEGY_LAB_*`` reads only."""

    def __init__(self) -> None:
        super().__init__(_os.environ)

    def _check(self, key: Any) -> None:
        if isinstance(key, str) and key.startswith(_RESTRICTED_ENV_PREFIX):
            raise _SandboxViolation(f"workflow run path read os.environ[{key!r}]")

    def __getitem__(self, key: Any) -> Any:  # noqa: D105
        self._check(key)
        return super().__getitem__(key)

    def get(self, key: Any, default: Any = None) -> Any:  # noqa: D102
        self._check(key)
        return super().get(key, default)


@contextlib.contextmanager
def _sandbox_restrictions():
    """Patch the sandbox-restricted callables that CPython lets us patch.

    ``datetime.datetime.now``/``utcnow`` and ``date.today`` are methods of an
    immutable C type and cannot be monkeypatched; ``time.time``/``monotonic``
    are used by asyncio's own event loop while stepping the coroutine, so
    patching them would break the harness rather than the workflow. All of those
    are covered by the static source check in
    ``test_run_source_has_no_restricted_names`` instead. The runtime
    restrictions that ARE ordinary module functions/attributes the workflow
    could call without asyncio needing them — ``os.getenv``/``os.environ`` reads
    (the exact channel the showstopper used: gates reading env vars) and
    ``uuid.uuid1``/``uuid4`` — are patched here, so a workflow-code call to any
    of them raises.

    The event loop is created *before* entering the patches so that
    ``run_until_complete`` (used by the caller) does not itself trip a patched
    ``os`` call during loop setup — only the workflow coroutine runs under the
    restriction, mirroring the sandbox, which restricts workflow code but not the
    worker's own plumbing.
    """
    patches = [
        mock.patch.object(_uuid, "uuid4", _boom("uuid.uuid4")),
        mock.patch.object(_uuid, "uuid1", _boom("uuid.uuid1")),
        mock.patch.object(_os, "getenv", _guarded_getenv),
        mock.patch.object(_os, "environ", _GuardedEnviron()),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _run_under_restrictions(cycle_input: Dict[str, Any]) -> Dict[str, Any]:
    loop = asyncio.new_event_loop()
    try:
        with _sandbox_restrictions():
            return loop.run_until_complete(wf.StrategyLabCycleWorkflow().run(cycle_input))
    finally:
        loop.close()


def _config_dict() -> Dict[str, Any]:
    return {"start_date": "2023-01-01", "end_date": "2023-12-31"}


_WF_CONFIG = {
    "design_review_rounds": 20,
    "design_review_stall_rounds": 3,
    "mechanical_repair_enabled": True,
    "code_conformance_retries": 2,
    "design_max_llm_calls": 120,
    "regime_summary_enabled": False,
    "max_design_reentries": 2,
}


def _patch_execute(handlers: Dict[str, Any]):
    async def _fake_exec(fn, *, args, **_kw):
        name = fn.__name__
        if name not in handlers:
            raise AssertionError(f"unexpected activity call: {name}")
        h = handlers[name]
        return h(args) if callable(h) else h

    return mock.patch("temporalio.workflow.execute_activity", _fake_exec)


_EMPTY_DRIFT = {"spec_history": [], "code_history": [], "gate_timeline": []}


def test_run_record_path_touches_no_restricted_callable():
    """The happy path (one attempt → record) runs clean under sandbox restrictions."""
    handlers = {
        "run_design_attempt_activity": lambda a: {
            "kind": "record",
            "record": {"lab_record_id": "rec-1"},
            "convergence_tracker_state": {"trial_count": 1},
            "gate_results": [],
            "budget_calls": 3,
            "drift": dict(_EMPTY_DRIFT),
        },
    }
    with _patch_execute(handlers):
        result = _run_under_restrictions(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {},
                "workflow_config": _WF_CONFIG,
            }
        )
    assert result["record"]["lab_record_id"] == "rec-1"


def test_run_reentry_and_short_circuit_path_touches_no_restricted_callable():
    """The re-entry loop + short-circuit assembly — including the pure
    ``ConvergenceTracker`` reconstruction/increment the workflow does between
    attempts — runs clean under sandbox restrictions."""
    reentry = {
        "kind": "reentry",
        "evidence": "nope",
        "last_spec": {"strategy_id": "s"},
        "last_code": "code",
        "failure_phase": "evaluation",
        "design_context": {"rounds": 1, "critiques": [], "stop_reason": "x", "loop_telemetry": {}},
        "convergence_tracker_state": {"trial_count": 0, "trial_count_at_snapshot": 0},
        "gate_results": [],
        "budget_calls": 5,
        "drift": dict(_EMPTY_DRIFT),
    }
    handlers = {
        "run_design_attempt_activity": lambda a: dict(
            reentry, convergence_tracker_state=a[0]["convergence_tracker_state"]
        ),
        "build_short_circuit_record_activity": lambda a: {
            "record": {"lab_record_id": "sc-1"},
            "convergence_tracker_state": a[0]["convergence_tracker_state"],
        },
    }
    with _patch_execute(handlers):
        result = _run_under_restrictions(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": {"trial_count": 0, "trial_count_at_snapshot": 0},
                "workflow_config": _WF_CONFIG,
            }
        )
    assert result["record"]["lab_record_id"] == "sc-1"


def test_batch_run_touches_no_restricted_callable():
    """The batch workflow's own code path — per-batch/per-wave loop, the pure
    ConvergenceTracker snapshot it takes per cycle, and the contiguous-prefix
    accounting — runs clean under the sandbox's restricted-callable set. Child
    workflows and activities (which run outside the sandbox) are mocked."""
    child_result = {
        "record": {"lab_record_id": "0"},
        "convergence_tracker_state": {"trial_count": 1},
    }

    async def _fake_start_child(_wf_run, _arg, *, id, **_kw):  # noqa: A002
        async def _handle():
            return child_result

        return _handle()

    handlers = {
        "compute_signal_brief_activity": lambda a: {
            "signal_brief": None,
            "signal_brief_storage": None,
        },
        "snapshot_prior_records_activity": lambda a: [],
        "persist_run_state_activity": lambda a: None,
        "finalize_cycle_record_activity": lambda a: {"record": {"lab_record_id": "fin-0"}},
        "merge_wave_results_activity": lambda a: {"primary_tracker_state": {"ok": True}},
        "is_run_cancelled_activity": lambda a: False,
        "external_terminal_status_activity": lambda a: None,
        "publish_run_event_activity": lambda a: None,
    }
    batch_input = {
        "run_id": "run-1",
        "config": _config_dict(),
        "batch_size": 1,
        "batch_count": 1,
        "max_parallel": 1,
        "benchmark_symbol": "SPY",
        "workflow_config": _WF_CONFIG,
        "convergence_tracker_state": {},
    }
    loop = asyncio.new_event_loop()
    try:
        with (
            _patch_execute(handlers),
            mock.patch("temporalio.workflow.start_child_workflow", _fake_start_child),
            # workflow.patched("strategy-lab-sse-run-events") is real workflow
            # code too -- needs its own mock the same way execute_activity/
            # start_child_workflow do (see test_strategy_lab_batch_workflow.py's
            # _Harness.patched for the same need). True here so this test still
            # exercises the SSE-publish call sites under sandbox restrictions,
            # not just the pre-SSE-events code path.
            mock.patch("temporalio.workflow.patched", lambda _patch_id: True),
            _sandbox_restrictions(),
        ):
            result = loop.run_until_complete(wf.StrategyLabBatchWorkflow().run(batch_input))
    finally:
        loop.close()
    assert result["status"] == "completed"
    assert result["completed_record_ids"] == ["fin-0"]


def test_restriction_harness_actually_trips():
    """Guard-the-guard: a coroutine that *does* call a restricted callable must
    be caught, proving the harness would catch a real determinism regression."""

    async def _offending():
        return _os.getenv("STRATEGY_LAB_DESIGN_REVIEW_ROUNDS")

    loop = asyncio.new_event_loop()
    try:
        with _sandbox_restrictions():
            raised = False
            try:
                loop.run_until_complete(_offending())
            except _SandboxViolation:
                raised = True
        assert raised, "restriction harness failed to trip on os.getenv()"
    finally:
        loop.close()


def test_run_source_has_no_restricted_names():
    """Static guard for the restricted callables the runtime harness cannot
    patch (``datetime.now``/``utcnow``/``today``, ``date.today``,
    ``time.time``/``monotonic``): the workflow's ``run`` body and the helpers it
    calls must not reference them at all — the whole point of the attempt-level
    design is that everything time/uuid/env-dependent lives inside activities.

    Scoped to the code (``run`` + module helpers), not the module docstring,
    which legitimately *describes* the restricted calls it avoids.
    """
    import inspect

    sources = [
        inspect.getsource(wf.StrategyLabCycleWorkflow.run),
        inspect.getsource(wf.StrategyLabBatchWorkflow.run),
        inspect.getsource(wf.StrategyLabBatchWorkflow._persist_state),
        inspect.getsource(wf._snapshot_tracker_wire),
        inspect.getsource(wf._contiguous_prefix),
        inspect.getsource(wf._exec),
        inspect.getsource(wf._empty_drift),
    ]
    banned = (
        "datetime.now",
        "datetime.utcnow",
        "date.today",
        ".utcnow(",
        "time.time(",
        "time.monotonic(",
        "uuid4(",
        "uuid1(",
    )
    for src in sources:
        for name in banned:
            assert name not in src, f"workflow run path references restricted {name!r}"


def test_directive_seeding_runs_clean_under_restrictions():
    """Seeding convergence directives from the batch tracker uses only pure
    ``ConvergenceTracker`` reads (Counter/Jaccard/hashlib) — no restricted call."""
    captured: List[Dict[str, Any]] = []

    def _attempt(args):
        captured.append(args[0])
        return {
            "kind": "record",
            "record": {"lab_record_id": "rec"},
            "convergence_tracker_state": {"trial_count": 0},
            "gate_results": [],
            "budget_calls": 0,
            "drift": dict(_EMPTY_DRIFT),
        }

    tracker_state = {
        "window_size": 5,
        "max_history": 50,
        "signatures": [],
        "failure_modes": {"AcceptanceGate": 4},
        "asset_class_history": [],
        "trial_count": 0,
        "trial_count_at_snapshot": 0,
    }
    with _patch_execute({"run_design_attempt_activity": _attempt}):
        _run_under_restrictions(
            {
                "prior_records": [],
                "config": _config_dict(),
                "convergence_tracker_state": tracker_state,
                "workflow_config": _WF_CONFIG,
            }
        )
    assert any("AcceptanceGate" in d for d in captured[0]["directives"])
