"""Tests for the deepthought Temporal wiring.

Covers the four pieces the runtime needs to actually dispatch through
Temporal instead of leaving the workflow as dead code:

1. ``temporal/__init__.py`` — the activity threads ``job_id`` through and owns
   the same job-store status transitions as the thread path, and the package
   no longer self-boots a worker at import.
2. ``temporal/worker.py`` — the no-arg boot function the team_service
   entrypoint calls via ``TEAM_TEMPORAL_WORKER_MODULE`` / ``_FUNC``.
3. ``temporal/start_workflow.py`` — the sync→async dispatch helper.
4. ``api/main.py`` — ``/deepthought/ask`` branches on ``is_temporal_enabled()``.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from deepthought.models import AgentResult, DeepthoughtResponse


def _sample_response() -> DeepthoughtResponse:
    return DeepthoughtResponse(
        answer="42",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="q",
            answer="42",
            confidence=0.9,
            child_results=[],
            was_decomposed=False,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )


# --------------------------------------------------------------------------- #
# temporal/__init__.py — activity job-store writeback + Pattern A shape
# --------------------------------------------------------------------------- #


def test_activity_writes_running_then_completed():
    """Happy path: activity flips RUNNING then COMPLETED with the result dump."""
    from deepthought.temporal import run_pipeline_activity

    mock_orch = MagicMock()
    mock_orch.process_message.return_value = _sample_response()

    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator", return_value=mock_orch),
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        result = run_pipeline_activity("job-1", {"message": "q"})

    assert result["answer"] == "42"
    statuses = [c.kwargs.get("status") for c in mock_update.call_args_list]
    assert statuses == ["running", "completed"]
    # The completed write carries the serialized result.
    assert mock_update.call_args_list[-1].kwargs["result"]["answer"] == "42"


def test_activity_writes_failed_and_reraises():
    """On orchestrator error the activity records FAILED then re-raises."""
    from deepthought.temporal import run_pipeline_activity

    mock_orch = MagicMock()
    mock_orch.process_message.side_effect = RuntimeError("boom")

    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator", return_value=mock_orch),
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
        pytest.raises(RuntimeError, match="boom"),
    ):
        run_pipeline_activity("job-2", {"message": "q"})

    statuses = [c.kwargs.get("status") for c in mock_update.call_args_list]
    assert statuses == ["running", "failed"]
    assert mock_update.call_args_list[-1].kwargs["error"] == "boom"


def test_activity_short_circuits_when_cancelled():
    """A job cancelled before the activity runs is never dispatched."""
    from deepthought.temporal import run_pipeline_activity

    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator") as mock_cls,
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=True),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        result = run_pipeline_activity("job-3", {"message": "q"})

    assert result == {}
    mock_cls.assert_not_called()
    mock_update.assert_not_called()


def test_activity_skips_completed_write_when_cancelled_mid_run():
    """Cancelled during the run: RUNNING is set, but COMPLETED is skipped."""
    from deepthought.temporal import run_pipeline_activity

    mock_orch = MagicMock()
    mock_orch.process_message.return_value = _sample_response()

    # Not cancelled at entry (first call), cancelled by the post-run check (second).
    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator", return_value=mock_orch),
        patch("deepthought.shared.job_store.is_job_cancelled", side_effect=[False, True]),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        result = run_pipeline_activity("job-3b", {"message": "q"})

    assert result["answer"] == "42"
    statuses = [c.kwargs.get("status") for c in mock_update.call_args_list]
    assert statuses == ["running"]  # RUNNING set, COMPLETED skipped


def test_activity_signature_takes_job_id_and_request():
    """Regression guard: the workflow passes (job_id, request)."""
    from deepthought.temporal import run_pipeline_activity

    params = list(inspect.signature(run_pipeline_activity).parameters)
    assert params == ["job_id", "request"]


def test_pattern_a_exports_present():
    import deepthought.temporal as dt

    assert dt.WORKFLOWS == [dt.DeepthoughtWorkflow]
    # The pipeline is decomposed into one activity per LLM boundary plus the
    # job-store transition activities; the legacy whole-pipeline activity is kept
    # for workflow.patched replay of in-flight histories.
    assert dt.ACTIVITIES == [
        dt.classify_strategy_activity,
        dt.analyse_activity,
        dt.force_direct_answer_activity,
        dt.deliberate_activity,
        dt.synthesise_activity,
        dt.start_job_activity,
        dt.is_cancelled_activity,
        dt.finalize_job_activity,
        dt.run_pipeline_activity,
    ]
    assert dt.run_pipeline_activity in dt.ACTIVITIES
    assert dt.TASK_QUEUE == "deepthought-queue"
    assert dt.WORKFLOW_ID_PREFIX == "deepthought-"


def test_importing_temporal_package_does_not_start_worker():
    """The package must not self-boot a worker at import (boot is worker.py)."""
    import shared.temporal

    for name in list(sys.modules):
        if name == "deepthought.temporal" or name.startswith("deepthought.temporal."):
            del sys.modules[name]

    with patch.object(shared.temporal, "start_team_worker") as patched:
        importlib.import_module("deepthought.temporal")
        assert patched.call_count == 0


# --------------------------------------------------------------------------- #
# temporal/worker.py
# --------------------------------------------------------------------------- #


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from deepthought.temporal.worker import start_deepthought_temporal_worker_thread

    assert start_deepthought_temporal_worker_thread() is False


def test_worker_start_delegates_when_enabled():
    from deepthought.temporal import worker

    with (
        patch.object(worker, "is_temporal_enabled", return_value=True),
        patch.object(worker, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker.start_deepthought_temporal_worker_thread() is True

    args, kwargs = mock_start.call_args
    assert args[0] == "deepthought"
    assert kwargs["task_queue"] == "deepthought-queue"


def test_worker_module_exposes_entrypoint_func():
    """team_service entrypoint resolves this name from the module by string."""
    from deepthought.temporal import worker

    assert callable(getattr(worker, "start_deepthought_temporal_worker_thread", None))


# --------------------------------------------------------------------------- #
# temporal/start_workflow.py
# --------------------------------------------------------------------------- #


def test_start_workflow_dispatches_with_prefixed_id():
    from deepthought.temporal import start_workflow as sw

    with patch.object(sw, "start_workflow_sync") as mock_start:
        sw.start_deepthought_workflow("abc", {"message": "q"})

    args, kwargs = mock_start.call_args
    assert args[0] is sw.DeepthoughtWorkflow.run
    assert args[1] == "abc"
    assert args[2] == {"message": "q"}
    assert kwargs["workflow_id"] == "deepthought-abc"
    assert kwargs["task_queue"] == "deepthought-queue"


# --------------------------------------------------------------------------- #
# api/main.py — dispatch branch
# --------------------------------------------------------------------------- #


def test_ask_uses_temporal_when_enabled():
    """With Temporal enabled, ``ask`` dispatches the workflow (no thread)."""
    from deepthought.api import main

    with (
        patch("shared.temporal.is_temporal_enabled", return_value=True),
        patch("deepthought.temporal.start_workflow.start_deepthought_workflow") as mock_start,
        patch.object(main, "create_job") as mock_create,
        patch("threading.Thread") as mock_thread,
    ):
        client = TestClient(main.app)
        resp = client.post("/deepthought/ask", json={"message": "q"})

    assert resp.status_code == 200
    # Both runtimes leave the job PENDING until it starts, so the submission
    # response matches the thread path (and the job store).
    assert resp.json()["status"] == "pending"
    mock_create.assert_called_once()
    mock_start.assert_called_once()
    mock_thread.assert_not_called()


def test_ask_marks_failed_when_temporal_start_raises():
    """A workflow-start failure must not orphan the job in PENDING.

    The job is flipped to FAILED and the error surfaces to the client rather
    than silently falling back to a thread.
    """
    from deepthought.api import main

    with (
        patch("shared.temporal.is_temporal_enabled", return_value=True),
        patch(
            "deepthought.temporal.start_workflow.start_deepthought_workflow",
            side_effect=RuntimeError("worker unreachable"),
        ),
        patch.object(main, "create_job"),
        patch.object(main, "update_job") as mock_update,
        patch("threading.Thread") as mock_thread,
    ):
        client = TestClient(main.app, raise_server_exceptions=False)
        resp = client.post("/deepthought/ask", json={"message": "q"})

    assert resp.status_code == 500
    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs["status"] == "failed"
    mock_thread.assert_not_called()


def test_ask_falls_back_to_thread_when_temporal_disabled():
    """With Temporal disabled, ``ask`` keeps the existing thread path."""
    from deepthought.api import main

    with (
        patch("shared.temporal.is_temporal_enabled", return_value=False),
        patch.object(main, "create_job"),
        patch.object(main, "_run_deepthought_background"),
        patch("threading.Thread") as mock_thread,
    ):
        client = TestClient(main.app)
        resp = client.post("/deepthought/ask", json={"message": "q"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    mock_thread.assert_called_once()


# --------------------------------------------------------------------------- #
# temporal/activities.py — per-LLM-boundary activities (decomposed pipeline)
# --------------------------------------------------------------------------- #


class _FakeLLM:
    """Offline LLM stub: canned text for the reasoning pass (``complete``)
    and canned JSON for the formatting pass (``complete_json``).
    """

    def __init__(self, *, json_return: dict | None = None, text_return: str = "TXT") -> None:
        self._json = json_return or {}
        self._text = text_return
        self.complete_calls = 0
        self.complete_json_calls = 0
        self.last_format_prompt = ""

    def complete_json(self, prompt="", *_a, **_k) -> dict:
        self.complete_json_calls += 1
        self.last_format_prompt = prompt
        return self._json

    def complete(self, *_a, **_k) -> str:
        self.complete_calls += 1
        return self._text


def _spec(**overrides):
    from deepthought.models import AgentSpec

    base = dict(
        agent_id="id-1",
        name="specialist",
        role_description="role",
        focus_question="fq",
        depth=1,
        parent_id="parent",
    )
    base.update(overrides)
    return AgentSpec(**base)


def _run_activity(fn, *args):
    from temporalio.testing import ActivityEnvironment

    return ActivityEnvironment().run(fn, *args)


def test_build_llm_exposes_complete_and_complete_json(monkeypatch):
    """Regression guard: ``_build_llm`` returns a real ``LLMClient``, not a bare
    ``strands.Agent`` (whose public surface is ``__call__``, not ``complete``/
    ``complete_json`` — every reasoning activity calling those would previously
    silently raise ``AttributeError``, swallowed by the callee's own broad
    ``except Exception`` fallback). ``LLM_PROVIDER=dummy`` exercises the real
    (unmocked) path without touching Postgres.
    """
    from deepthought.temporal import activities

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    llm = activities._build_llm()
    assert callable(llm.complete)
    assert callable(llm.complete_json)
    assert isinstance(llm.complete("hello", objective="test"), str)
    assert isinstance(llm.complete_json("hello", objective="test"), dict)


def test_classify_strategy_activity_returns_value():
    """The activity resolves the strategy via the orchestrator and returns its value."""
    from deepthought.models import DecompositionStrategy
    from deepthought.temporal import activities

    captured: dict = {}

    class _FakeOrchestrator:
        def __init__(self, *, llm=None):
            captured["llm"] = llm

        def _resolve_strategy(self, _req):
            return DecompositionStrategy.BY_CONCERN

    sentinel_llm = object()
    with (
        patch("deepthought.orchestrator.DeepthoughtOrchestrator", _FakeOrchestrator),
        patch.object(activities, "_build_llm", return_value=sentinel_llm),
    ):
        out = _run_activity(activities.classify_strategy_activity, {"message": "q"})

    assert out == "by_concern"
    # The cached (shared) client is threaded into the orchestrator rather than
    # letting it build its own.
    assert captured["llm"] is sentinel_llm


def test_analyse_activity_direct_answer():
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import AnalysePayload

    fake = _FakeLLM(
        json_return={
            "summary": "s",
            "can_answer_directly": True,
            "direct_answer": "the answer",
            "confidence": 0.8,
        }
    )
    payload = AnalysePayload(
        spec=_spec(), original_query="q", decomposition_strategy="auto", max_depth=3
    ).model_dump(mode="json")

    with patch.object(activities, "_build_llm", return_value=fake):
        out = _run_activity(activities.analyse_activity, payload)

    assert out["can_answer_directly"] is True
    assert out["direct_answer"] == "the answer"
    # Two-call split: exactly one reasoning pass (.complete) feeding exactly
    # one formatting pass (.complete_json), and the reasoning output (TXT,
    # this fake's canned text_return) reaches the formatting prompt.
    assert fake.complete_calls == 1
    assert fake.complete_json_calls == 1
    assert "TXT" in fake.last_format_prompt


def test_analyse_activity_injects_knowledge_summary():
    """The bounded, pre-rendered summary is injected into the analysis system prompt."""
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import AnalysePayload

    captured: dict = {}

    class _CapturingLLM(_FakeLLM):
        # The knowledge_summary now reaches the analysis reasoning-pass
        # system prompt (the .complete() call), not the formatting call's.
        def complete(self, user, *a, **k):
            self.complete_calls += 1
            captured["system"] = k.get("system_prompt", "")
            return "TXT"

        def complete_json(self, prompt="", *a, **k):
            self.complete_json_calls += 1
            captured["format_system"] = k.get("system_prompt", "") or ""
            captured["format_prompt"] = prompt
            return {"can_answer_directly": True, "direct_answer": "ok", "confidence": 0.5}

    fake = _CapturingLLM()
    payload = AnalysePayload(
        spec=_spec(),
        original_query="q",
        decomposition_strategy="auto",
        knowledge_summary="- [prior] an earlier finding worth reusing",
        max_depth=3,
    ).model_dump(mode="json")

    with patch.object(activities, "_build_llm", return_value=fake):
        out = _run_activity(activities.analyse_activity, payload)

    assert out["can_answer_directly"] is True
    assert fake.complete_calls == 1
    assert fake.complete_json_calls == 1
    # The workflow-rendered summary reaches the prompt verbatim — no per-node KB
    # is shipped to the activity.
    assert "an earlier finding worth reusing" in captured["system"]
    # ...and never leaks into the formatting call, which only sees the
    # reasoning pass's prose.
    assert "an earlier finding worth reusing" not in captured["format_system"]
    assert "an earlier finding worth reusing" not in captured["format_prompt"]


def test_analyse_activity_decomposition():
    """Decomposition strategy yields skill_requirements and uses the two-call split."""
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import AnalysePayload

    fake = _FakeLLM(
        json_return={
            "can_answer_directly": False,
            "skill_requirements": [
                {"name": "n", "description": "d", "focus_question": "fq2", "reasoning": "r"}
            ],
        }
    )
    payload = AnalysePayload(
        spec=_spec(depth=0), original_query="q", decomposition_strategy="auto", max_depth=3
    ).model_dump(mode="json")

    with patch.object(activities, "_build_llm", return_value=fake):
        out = _run_activity(activities.analyse_activity, payload)

    assert out["can_answer_directly"] is False
    assert len(out["skill_requirements"]) == 1
    assert fake.complete_calls == 1
    assert fake.complete_json_calls == 1
    assert "TXT" in fake.last_format_prompt


def test_force_direct_answer_activity_returns_text():
    """force_direct_answer_activity returns the LLM's forced-answer text."""
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import ForceDirectAnswerPayload

    payload = ForceDirectAnswerPayload(spec=_spec(), original_query="q").model_dump(mode="json")
    with patch.object(activities, "_build_llm", return_value=_FakeLLM(text_return="forced")):
        out = _run_activity(activities.force_direct_answer_activity, payload)

    assert out == "forced"


def test_deliberate_activity_returns_notes():
    """deliberate_activity returns the LLM deliberation notes string."""
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import ChildSummary, DeliberatePayload

    children = [
        ChildSummary(agent_name="a", focus_question="qa", answer="aa", confidence=0.7),
        ChildSummary(agent_name="b", focus_question="qb", answer="bb", confidence=0.6),
    ]
    payload = DeliberatePayload(spec=_spec(), original_query="q", children=children).model_dump(
        mode="json"
    )
    with patch.object(activities, "_build_llm", return_value=_FakeLLM(text_return="notes")):
        out = _run_activity(activities.deliberate_activity, payload)

    assert out == "notes"


def test_synthesise_activity_returns_answer():
    """synthesise_activity returns the LLM's synthesised answer string."""
    from deepthought.temporal import activities
    from deepthought.temporal.phase_models import ChildSummary, SynthesisePayload

    children = [ChildSummary(agent_name="a", focus_question="qa", answer="aa", confidence=0.7)]
    payload = SynthesisePayload(
        spec=_spec(), original_query="q", deliberation_notes="notes", children=children
    ).model_dump(mode="json")
    with patch.object(activities, "_build_llm", return_value=_FakeLLM(text_return="final")):
        out = _run_activity(activities.synthesise_activity, payload)

    assert out == "final"


def test_start_job_activity_flips_running():
    from deepthought.temporal import activities

    with (
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        assert _run_activity(activities.start_job_activity, "job-1") is True

    assert mock_update.call_args.kwargs["status"] == "running"


def test_start_job_activity_short_circuits_when_cancelled():
    from deepthought.temporal import activities

    with (
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=True),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        assert _run_activity(activities.start_job_activity, "job-1") is False

    mock_update.assert_not_called()


def test_is_cancelled_activity_reports_job_store():
    from deepthought.temporal import activities

    with patch("deepthought.shared.job_store.is_job_cancelled", return_value=True):
        assert _run_activity(activities.is_cancelled_activity, "job-1") is True
    with patch("deepthought.shared.job_store.is_job_cancelled", return_value=False):
        assert _run_activity(activities.is_cancelled_activity, "job-1") is False


def test_finalize_job_activity_writes_completed():
    from deepthought.temporal import activities

    with (
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        _run_activity(activities.finalize_job_activity, "job-1", {"answer": "A"}, True, "")

    assert mock_update.call_args.kwargs["status"] == "completed"
    assert mock_update.call_args.kwargs["result"] == {"answer": "A"}


def test_finalize_job_activity_writes_failed():
    from deepthought.temporal import activities

    with (
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=False),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        _run_activity(activities.finalize_job_activity, "job-1", {}, False, "boom")

    assert mock_update.call_args.kwargs["status"] == "failed"
    assert mock_update.call_args.kwargs["error"] == "boom"


def test_finalize_job_activity_skips_when_cancelled():
    from deepthought.temporal import activities

    with (
        patch("deepthought.shared.job_store.is_job_cancelled", return_value=True),
        patch("deepthought.shared.job_store.update_job") as mock_update,
    ):
        _run_activity(activities.finalize_job_activity, "job-1", {"answer": "A"}, True, "")

    mock_update.assert_not_called()
