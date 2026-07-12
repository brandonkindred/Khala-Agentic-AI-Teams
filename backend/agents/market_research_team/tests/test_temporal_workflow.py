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
    """A minimal awaitable standing in for a Temporal ActivityHandle.

    Mirrors the real ``ActivityHandle``'s ``cancel()``: it does not resolve the
    result immediately, it marks the handle so the next await raises
    ``asyncio.CancelledError`` — matching how a cancelled ``asyncio.Task``
    behaves, which the workflow's ``with suppress(BaseException): await
    scripts_handle`` after ``.cancel()`` relies on.
    """

    def __init__(self, result, error=None):
        self._result = result
        self._error = error
        self._cancelled = False

    def cancel(self) -> bool:
        self._cancelled = True
        return True

    def __await__(self):
        async def _coro():
            if self._cancelled:
                raise asyncio.CancelledError()
            if self._error is not None:
                raise self._error
            return self._result

        return _coro().__await__()


class _Recorder:
    """Records activity scheduling and returns canned results keyed by name."""

    def __init__(self, results, raise_for=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = results
        self.raise_for = raise_for or {}
        # The workflow only ever starts one activity via ``start_activity``
        # (``scripts``), so a single slot is enough to let tests assert on the
        # handle it was given (e.g. that ``.cancel()`` was called on it).
        self.started_handle: _Handle | None = None

    async def execute_activity(self, fn, *args, **kwargs):
        name = _act_name(fn)
        # Mirror the real ``workflow.execute_activity(activity, *args, **kwargs)``
        # signature. This workflow always passes activity inputs via the
        # ``args=[...]`` keyword (so positional ``args`` is empty and the keyword
        # value below wins), but capturing positional args keeps the mock
        # faithful and lets ``kwargs_for(...)["args"]`` read them either way.
        # Note: unregistered names intentionally return ``None`` — void
        # activities like ``mark_failed``/``report_progress`` are not seeded with
        # a result and legitimately return ``None``/a bool.
        self.calls.append((name, {"args": args, **kwargs}))
        if name in self.raise_for:
            raise self.raise_for[name]
        return self.results.get(name)

    def start_activity(self, fn, *args, **kwargs):
        name = _act_name(fn)
        self.calls.append((name, {"args": args, **kwargs}))
        # Mirrors the real ActivityHandle: starting never raises, a scheduled
        # failure only surfaces when the handle is later awaited.
        if name not in self.results and name not in self.raise_for:
            raise KeyError(f"Unknown activity started (not seeded in results): {name}")
        self.started_handle = _Handle(self.results.get(name), error=self.raise_for.get(name))
        return self.started_handle

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
        # ingest returns lightweight refs (bodies live in the per-job store).
        "market_research_ingest": [
            {"index": 0, "source": "s1"},
            {"index": 1, "source": "s2"},
        ],
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


def _install(monkeypatch, recorder: _Recorder, *, patched: bool = True) -> None:
    monkeypatch.setattr(wf.workflow, "execute_activity", recorder.execute_activity)
    monkeypatch.setattr(wf.workflow, "start_activity", recorder.start_activity)
    # ``workflow.patched`` needs a real workflow event loop; stub it to select the
    # new per-stage DAG (True) or the legacy drain-out branch (False).
    monkeypatch.setattr(wf.workflow, "patched", lambda *a, **k: patched)
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


def test_workflow_fails_when_all_ux_transcripts_dropped(monkeypatch) -> None:
    """Transcripts were loaded but EVERY UX activity failed (all dropped by
    gather) and the job is still active → the run fails (rather than silently
    reporting insufficient evidence), recording FAILED via mark_failed."""
    rec = _Recorder(
        _default_results(),
        raise_for={"market_research_ux_one": RuntimeError("ux boom")},
    )
    _install(monkeypatch, rec)

    with pytest.raises(RuntimeError, match="All 2 transcript analyses failed"):
        _run(_REQUEST_UNIFIED)

    assert "market_research_mark_failed" in rec.names()
    # It fails before scheduling the downstream fan-in / finalize.
    assert "market_research_viability" not in rec.names()
    assert "market_research_finalize" not in rec.names()


def test_workflow_no_transcripts_completes_without_failing(monkeypatch) -> None:
    """No transcripts loaded → no UX fan-out, run completes normally (an empty
    corpus is a valid 'insufficient evidence' result, NOT an analysis failure)."""
    results = _default_results()
    results["market_research_ingest"] = []
    rec = _Recorder(results)
    _install(monkeypatch, rec)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    assert rec.count("market_research_ux_one") == 0
    assert rec.kwargs_for("market_research_viability")["args"][2] == 0
    assert "market_research_finalize" in rec.names()
    assert "market_research_mark_failed" not in rec.names()


def test_root_cause_message_unwraps_chained_exception() -> None:
    root = ValueError("real root cause")
    wrapper = RuntimeError("Activity task failed")
    wrapper.__cause__ = root

    assert wf._root_cause_message(wrapper) == "real root cause"


def test_root_cause_message_returns_str_when_unchained() -> None:
    assert wf._root_cause_message(RuntimeError("plain failure")) == "plain failure"


def test_workflow_mark_failed_receives_root_cause_not_wrapper_text(monkeypatch) -> None:
    """Regression: ``execute_activity`` failures surface as an ``ActivityError``
    wrapper whose own ``str()`` is generic boilerplate — the workflow's
    catch-all must pass the innermost chained message to ``mark_failed``, not
    the wrapper's text."""
    wrapper = RuntimeError("Activity task failed")
    wrapper.__cause__ = ValueError("viability blew up for real")
    rec = _Recorder(
        _default_results(),
        raise_for={"market_research_viability": wrapper},
    )
    _install(monkeypatch, rec)

    with pytest.raises(RuntimeError, match="Activity task failed"):
        _run(_REQUEST_UNIFIED)

    assert rec.kwargs_for("market_research_mark_failed")["args"][1] == "viability blew up for real"


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


def test_workflow_cancels_scripts_handle_on_stage_failure(monkeypatch) -> None:
    """A fatal error after ``scripts`` was started must cancel + drain that
    independently-scheduled handle instead of leaving it orphaned."""
    rec = _Recorder(
        _default_results(),
        raise_for={"market_research_viability": RuntimeError("viability boom")},
    )
    _install(monkeypatch, rec)

    with pytest.raises(RuntimeError, match="viability boom"):
        _run(_REQUEST_UNIFIED)

    assert rec.started_handle._cancelled is True


def test_workflow_scripts_failure_is_best_effort(monkeypatch) -> None:
    """The scripts activity failing (after every other stage succeeded) must
    not fail the whole run — finalize still runs, with an empty scripts list."""
    rec = _Recorder(
        _default_results(),
        raise_for={"market_research_scripts": RuntimeError("scripts boom")},
    )
    _install(monkeypatch, rec)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    assert rec.kwargs_for("market_research_finalize")["args"][4] == []
    assert "market_research_mark_failed" not in rec.names()


def test_workflow_drain_out_runs_legacy_activity_when_unpatched(monkeypatch) -> None:
    """A pre-decomposition history (workflow.patched → False) re-schedules the
    legacy whole-pipeline activity with byte-identical options — and nothing
    from the new DAG — so in-flight runs survive the deploy."""
    rec = _Recorder({"market_research_run_pipeline": {"job_id": "job-x"}})
    _install(monkeypatch, rec, patched=False)

    out = _run(_REQUEST_UNIFIED)

    assert out == {"job_id": "job-x"}
    assert rec.names() == ["market_research_run_pipeline"]
    legacy = rec.kwargs_for("market_research_run_pipeline")
    assert legacy["args"] == ["job-x", _REQUEST_UNIFIED]
    assert legacy["retry_policy"] is wf._LEGACY_ACTIVITY_RETRY
    assert legacy["start_to_close_timeout"] == wf._LEGACY_ACTIVITY_TIMEOUT


def test_workflow_prepare_receives_stripped_request(monkeypatch) -> None:
    """Inline transcripts are stripped before prepare (not recorded in its
    activity input); ingest still receives the full request to load them."""
    rec = _Recorder(_default_results())
    _install(monkeypatch, rec)
    request = {**_REQUEST_UNIFIED, "transcripts": ["a body"], "transcript_folder_path": "/x"}

    _run(request)

    prep = rec.kwargs_for("market_research_prepare")["args"][1]
    assert prep["transcripts"] == []
    assert prep["transcript_folder_path"] is None
    ingest = rec.kwargs_for("market_research_ingest")["args"][1]
    assert ingest["transcripts"] == ["a body"]
