"""Tests for the fine-grained ``MarketResearchWorkflow`` orchestration.

These exercise the deterministic workflow body in isolation by monkeypatching
``workflow.execute_activity`` / ``workflow.start_activity`` with recorders, so no
Temporal server or worker is needed. They pin the DAG (prepare → progress-gate →
ingest → per-transcript UX fan-out → psychology [+consistency in split] →
viability → finalize), the split-vs-unified branch, the per-activity retry
policies, the cancel short-circuits, and the catch-all FAILED contract.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from market_research_team.temporal import workflows as wf

_REQUEST_UNIFIED = {
    "product_concept": "Interview analysis assistant",
    "target_users": "startup founders",
    "business_goal": "validate demand faster",
    "topology": "unified",
}
_REQUEST_SPLIT = {**_REQUEST_UNIFIED, "topology": "split"}

_UX_INSIGHT = {
    "source": "s",
    "user_jobs": [],
    "pain_points": [],
    "desired_outcomes": [],
    "direct_quotes": [],
}


def _act_name(fn) -> str:
    return fn.__temporal_activity_definition.name


class _Handle:
    """A minimal awaitable standing in for a Temporal ActivityHandle."""

    def __init__(self, result):
        self._result = result

    def __await__(self):
        async def _coro():
            return self._result

        return _coro().__await__()


class _Recorder:
    """Records activity scheduling and returns canned results keyed by name."""

    def __init__(self, results, raise_for=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results
        self.raise_for = raise_for or {}

    async def execute_activity(self, fn, **kwargs):
        name = _act_name(fn)
        self.calls.append((name, kwargs))
        if name in self.raise_for:
            raise self.raise_for[name]
        return self.results.get(name)

    def start_activity(self, fn, **kwargs):
        name = _act_name(fn)
        self.calls.append((name, kwargs))
        return _Handle(self.results.get(name))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def kwargs_for(self, name: str) -> dict:
        return next(kw for n, kw in self.calls if n == name)

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def _default_results(*, ctx_stopped=False, progress=True):
    return {
        "market_research_prepare": {
            "stopped": ctx_stopped,
            "job_id": "job-x",
            "request": _REQUEST_UNIFIED,
        },
        "market_research_report_progress": progress,
        "market_research_ingest": [["s1", "t1"], ["s2", "t2"]],
        "market_research_ux_one": dict(_UX_INSIGHT),
        "market_research_psychology": [
            {"signal": "a", "confidence": 0.5, "evidence": []},
            {"signal": "b", "confidence": 0.5, "evidence": []},
        ],
        "market_research_consistency": [
            {"signal": "Cross-interview theme consistency", "confidence": 0.55, "evidence": []}
        ],
        "market_research_viability": {
            "verdict": "needs_more_validation",
            "confidence": 0.5,
            "rationale": [],
            "suggested_next_experiments": [],
        },
        "market_research_scripts": ["script1"],
        "market_research_finalize": {"job_id": "job-x"},
    }


def _install(monkeypatch, recorder: _Recorder) -> None:
    monkeypatch.setattr(wf.workflow, "execute_activity", recorder.execute_activity)
    monkeypatch.setattr(wf.workflow, "start_activity", recorder.start_activity)
    # The catch-all logs via ``workflow.logger``, which needs a real workflow
    # event loop; outside one (these unit tests) a MagicMock stands in for it.
    monkeypatch.setattr(wf.workflow, "logger", MagicMock())


def _run(request):
    return asyncio.run(wf.MarketResearchWorkflow().run("job-x", request))


def test_workflow_unified_orchestration(monkeypatch) -> None:
    rec = _Recorder(_default_results())
    _install(monkeypatch, rec)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    names = rec.names()
    assert names[0] == "market_research_prepare"
    assert names[-1] == "market_research_finalize"
    # UX fans out one activity per transcript (two loaded).
    assert rec.count("market_research_ux_one") == 2
    # Unified topology: consistency is NOT scheduled.
    assert "market_research_consistency" not in names
    assert "market_research_psychology" in names
    assert "market_research_scripts" in names
    # Viability receives the count of successful insights.
    assert rec.kwargs_for("market_research_viability")["args"][2] == 2


def test_workflow_split_schedules_consistency(monkeypatch) -> None:
    results = _default_results()
    results["market_research_prepare"]["request"] = _REQUEST_SPLIT
    rec = _Recorder(results)
    _install(monkeypatch, rec)

    _run(_REQUEST_SPLIT)

    assert "market_research_consistency" in rec.names()


def test_workflow_prepare_stopped_short_circuits(monkeypatch) -> None:
    rec = _Recorder(_default_results(ctx_stopped=True))
    _install(monkeypatch, rec)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    assert rec.names() == ["market_research_prepare"]


def test_workflow_progress_gate_stops_when_terminal(monkeypatch) -> None:
    rec = _Recorder(_default_results(progress=False))
    _install(monkeypatch, rec)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    # Gate is checked right after prepare, before any ingest/UX spend.
    assert rec.names() == ["market_research_prepare", "market_research_report_progress"]


def test_workflow_drops_failed_ux_transcripts(monkeypatch) -> None:
    """Every UX activity failing → insights collapse to empty (gather drops
    them), yet the run still completes (viability sees count 0)."""
    rec = _Recorder(
        _default_results(),
        raise_for={"market_research_ux_one": RuntimeError("ux boom")},
    )
    _install(monkeypatch, rec)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    assert rec.kwargs_for("market_research_viability")["args"][2] == 0
    assert "market_research_finalize" in rec.names()


def test_workflow_marks_failed_and_reraises_on_stage_error(monkeypatch) -> None:
    rec = _Recorder(
        _default_results(),
        raise_for={"market_research_finalize": RuntimeError("finalize boom")},
    )
    _install(monkeypatch, rec)

    with pytest.raises(RuntimeError, match="finalize boom"):
        _run(_REQUEST_UNIFIED)

    # The catch-all records FAILED via the dedicated activity before re-raising.
    assert "market_research_mark_failed" in rec.names()
    assert rec.kwargs_for("market_research_mark_failed")["args"][0] == "job-x"


def test_workflow_retry_policies_and_timeouts(monkeypatch) -> None:
    rec = _Recorder(_default_results())
    _install(monkeypatch, rec)

    _run(_REQUEST_UNIFIED)

    # Cheap job-store activities use the aggressive IO retry (5s initial).
    prepare_rp = rec.kwargs_for("market_research_prepare")["retry_policy"]
    assert prepare_rp is wf.IO_RETRY
    assert prepare_rp.maximum_attempts == 3
    # Long LLM activities use the slower LLM retry (30s initial) + a heartbeat.
    ux_kwargs = rec.kwargs_for("market_research_ux_one")
    assert ux_kwargs["retry_policy"] is wf.LLM_RETRY
    assert ux_kwargs["heartbeat_timeout"] == wf._HEARTBEAT_TIMEOUT
    assert wf.LLM_RETRY.initial_interval.total_seconds() == 30
    assert wf.IO_RETRY.initial_interval.total_seconds() == 5


def test_workflow_mark_failed_failure_is_swallowed(monkeypatch) -> None:
    """If recording FAILED itself fails, the ORIGINAL pipeline error still
    propagates (the mark-failed failure is swallowed)."""
    rec = _Recorder(
        _default_results(),
        raise_for={
            "market_research_viability": RuntimeError("viability boom"),
            "market_research_mark_failed": RuntimeError("store down"),
        },
    )
    _install(monkeypatch, rec)

    with pytest.raises(RuntimeError, match="viability boom"):
        _run(_REQUEST_UNIFIED)
