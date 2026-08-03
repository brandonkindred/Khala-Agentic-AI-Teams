"""Unit + integration tests for the Branding team's per-phase Temporal wiring.

These tests do not need a live Temporal server. They mock at the ``temporalio`` /
``shared.temporal`` boundary and verify:

* the ``constants`` / ``worker`` / ``start_workflow`` surfaces honor their
  contracts;
* each decomposed activity honors its contract (called as a plain function with
  the orchestrator/job-store/checkpoint boundary patched);
* ``orchestrator.run_single_phase`` builds a single-node phase graph, injects
  upstream context, and extracts the phase output;
* ``BrandingWorkflow.run`` sequences the activities correctly — phase order,
  ``prior_outputs`` threading, integration gating, cancel, and failure handling
  (driven by monkeypatching ``workflow.execute_activity``); and
* ``_submit_brand_run`` still routes through Temporal when enabled (and
  surfaces a dispatch failure without falling through to the thread pool).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from branding_team.models import BrandPhase
from branding_team.tests.conftest import make_mission

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_task_queue_default() -> None:
    from branding_team.temporal import constants

    assert isinstance(constants.TASK_QUEUE, str)
    assert constants.TASK_QUEUE == "branding-queue"
    assert constants.WORKFLOW_ID_PREFIX == "branding-"


def test_task_queue_env_override(monkeypatch) -> None:
    import importlib

    from branding_team.temporal import constants as constants_mod

    monkeypatch.setenv("TEMPORAL_TASK_QUEUE_BRANDING", "custom-branding")
    reloaded = importlib.reload(constants_mod)
    try:
        assert reloaded.TASK_QUEUE == "custom-branding"
    finally:
        monkeypatch.delenv("TEMPORAL_TASK_QUEUE_BRANDING", raising=False)
        importlib.reload(reloaded)


def test_phase_sequence_matches_brand_phase_values() -> None:
    """PHASE_SEQUENCE must be the five runnable BrandPhase values, in order —
    the workflow indexes it and the finalize activity maps it to ``stop_idx``.

    This is the Temporal-side half of the ``PHASE_ORDER`` drift guard; its
    SYSTEM_PROMPT-side sibling is
    ``test_prompts.py::test_system_prompt_sections_follow_reordered_phase_order``.
    """
    from branding_team.temporal.constants import PHASE_SEQUENCE

    assert PHASE_SEQUENCE == [
        BrandPhase.STRATEGIC_CORE.value,
        BrandPhase.NARRATIVE_MESSAGING.value,
        BrandPhase.VISUAL_IDENTITY.value,
        BrandPhase.CHANNEL_ACTIVATION.value,
        BrandPhase.GOVERNANCE.value,
    ]


def test_phase_sequence_derives_from_canonical_phase_order() -> None:
    """PHASE_SEQUENCE (temporal, value strings) must not drift from the canonical
    PHASE_ORDER (graphs/shared, enums). Asserting equality here catches a reorder
    or insertion in one that is not mirrored in the other.

    See ``test_prompts.py::test_system_prompt_sections_follow_reordered_phase_order``
    for the equivalent drift guard on the SYSTEM_PROMPT side.
    """
    from branding_team.graphs.shared import PHASE_ORDER
    from branding_team.temporal.constants import PHASE_SEQUENCE

    assert PHASE_SEQUENCE == [p.value for p in PHASE_ORDER]


def test_stop_index_maps_non_runnable_phase_to_all() -> None:
    """stop_index maps None / COMPLETE / any non-runnable value to the last phase
    (run all), mirroring thread mode's phase_index fallback — the guard that keeps
    target_phase='complete' from raising ValueError in the workflow."""
    from branding_team.temporal.constants import PHASE_SEQUENCE, stop_index

    last = len(PHASE_SEQUENCE) - 1
    assert stop_index(None) == last
    assert stop_index("complete") == last  # BrandPhase.COMPLETE — not a pipeline phase
    assert stop_index("bogus") == last
    assert stop_index("strategic_core") == 0
    assert stop_index("governance") == last


# ---------------------------------------------------------------------------
# worker.py
# ---------------------------------------------------------------------------


def test_start_worker_thread_no_op_when_disabled() -> None:
    import shared.temporal
    from branding_team.temporal import worker as worker_mod

    # Patch the delegate too: start_team_worker has its own gate, so asserting
    # it is NOT called proves the early return comes from the function's guard.
    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=False),
        patch.object(shared.temporal, "start_team_worker") as mock_start,
    ):
        assert worker_mod.start_branding_temporal_worker_thread() is False
        mock_start.assert_not_called()


def test_start_worker_thread_delegates_to_start_team_worker() -> None:
    """The entrypoint contract (TEAM_TEMPORAL_WORKER_FUNC) resolves to a real
    function that boots the worker via shared.temporal."""
    import shared.temporal
    from branding_team.temporal import worker as worker_mod

    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=True),
        patch.object(shared.temporal, "start_team_worker", return_value=True) as mock_start,
    ):
        assert worker_mod.start_branding_temporal_worker_thread() is True
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        assert args[0] == "branding"
        assert kwargs["task_queue"] == worker_mod.TASK_QUEUE


# ---------------------------------------------------------------------------
# registration (__init__ exports)
# ---------------------------------------------------------------------------


def test_registration_exports_workflow_and_all_activities() -> None:
    from branding_team.temporal import ACTIVITIES, WORKFLOWS
    from branding_team.temporal.workflows import BrandingWorkflow

    assert WORKFLOWS == [BrandingWorkflow]
    names = {a.__name__ for a in ACTIVITIES}
    assert names == {
        "begin_branding_job_activity",
        "run_branding_phase_activity",
        "run_market_research_activity",
        "run_design_assets_activity",
        "finalize_branding_activity",
        "mark_branding_failed_activity",
        "check_branding_cancelled_activity",
    }


# ---------------------------------------------------------------------------
# start_workflow.py
# ---------------------------------------------------------------------------


def test_start_branding_workflow_delegates_to_start_workflow_sync() -> None:
    """The dispatcher is a thin wrapper over shared.temporal.start_workflow_sync
    (which owns the client-ready wait), forwarding the payload and a
    deterministic workflow id on the branding task queue."""
    from branding_team.temporal import start_workflow as sw
    from branding_team.temporal.constants import TASK_QUEUE, WORKFLOW_ID_PREFIX
    from branding_team.temporal.workflows import BrandingWorkflow

    with patch.object(sw, "start_workflow_sync") as mock_sync:
        payload = {"job_id": "job-9"}
        sw.start_branding_workflow("job-9", payload)

    mock_sync.assert_called_once()
    args, kwargs = mock_sync.call_args
    assert args[0] is BrandingWorkflow.run
    # payload must be forwarded as the single workflow arg (position 1), not just
    # present somewhere in the tuple.
    assert args[1] is payload
    assert kwargs["workflow_id"] == f"{WORKFLOW_ID_PREFIX}job-9"
    assert kwargs["task_queue"] == TASK_QUEUE


def test_start_branding_workflow_propagates_client_error() -> None:
    """start_workflow_sync raises RuntimeError when the worker client never
    becomes available; the dispatcher must let that surface."""
    from branding_team.temporal import start_workflow as sw

    with patch.object(sw, "start_workflow_sync", side_effect=RuntimeError("no client")):
        with pytest.raises(RuntimeError, match="no client"):
            sw.start_branding_workflow("job-1", {"job_id": "job-1"})


# ---------------------------------------------------------------------------
# activities.py — each decomposed activity's contract (called as plain functions)
# ---------------------------------------------------------------------------


def _mission_dict() -> dict:
    """Minimal mission payload dict derived from ``make_mission`` defaults."""
    mission = make_mission()
    return {
        "company_name": mission.company_name,
        "company_description": mission.company_description,
        "target_audience": mission.target_audience,
    }


def _phase_payload(**overrides) -> dict:
    payload = {
        "job_id": "job-abc",
        "mission": _mission_dict(),
        "human_review": {"approved": True, "feedback": "go"},
        "brand_checks": [],
        "client_id": "c1",
        "brand_id": "b1",
        "include_market_research": False,
        "include_design_assets": False,
        "target_phase": None,
    }
    payload.update(overrides)
    return payload


def test_begin_activity_runs_then_returns_true() -> None:
    from branding_team.api import main as main_mod
    from branding_team.temporal import activities

    with patch(
        "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
    ) as mock_update:
        assert activities.begin_branding_job_activity("job-1") is True

    _, kwargs = mock_update.call_args
    assert kwargs["status"] == main_mod.JOB_STATUS_RUNNING


def test_begin_activity_returns_false_when_cancelled() -> None:
    from branding_team.temporal import activities

    with patch("branding_team.shared.job_store.update_job_if_not_cancelled", return_value=False):
        assert activities.begin_branding_job_activity("job-1") is False


def test_begin_activity_raises_job_not_found_when_missing() -> None:
    from branding_team.shared.job_store import JobNotFoundError
    from branding_team.temporal import activities

    with patch("branding_team.shared.job_store.update_job_if_not_cancelled", return_value=None):
        with pytest.raises(JobNotFoundError):
            activities.begin_branding_job_activity("missing-job")


def test_phase_activity_runs_phase_and_checkpoints(monkeypatch) -> None:
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.models import StrategicCoreOutput
    from branding_team.temporal import activities

    model = StrategicCoreOutput(positioning_statement="POS-123")
    calls: list = []
    monkeypatch.setattr(shared.temporal, "load_checkpoint", lambda team, jid, phase: None)
    monkeypatch.setattr(
        shared.temporal,
        "save_checkpoint",
        lambda team, jid, phase, payload: calls.append(
            {"team": team, "jid": jid, "phase": phase, "payload": payload}
        ),
    )

    with patch.object(
        main_mod.orchestrator, "run_single_phase", return_value=(model, False)
    ) as mock_rsp:
        out = activities.run_branding_phase_activity(_phase_payload(), "strategic_core", {})

    assert out["positioning_statement"] == "POS-123"
    mock_rsp.assert_called_once()
    # The degradation flag is checkpointed before the output, so a crash between
    # the two writes can never leave an output checkpoint without its paired flag.
    assert [c["phase"] for c in calls] == ["strategic_core__degraded", "strategic_core"]
    degraded_call, output_call = calls
    # Checkpoint must use the branding_team job slug (the row's actual slug), not
    # the "branding" worker slug.
    assert degraded_call["team"] == "branding_team"
    assert degraded_call["jid"] == "job-abc"
    assert degraded_call["payload"] is False
    assert output_call["team"] == "branding_team"
    assert output_call["jid"] == "job-abc"
    assert output_call["payload"]["positioning_statement"] == "POS-123"


def test_phase_activity_checkpoint_short_circuits(monkeypatch) -> None:
    """A pre-existing checkpoint short-circuits the (expensive) phase re-run."""
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.temporal import activities

    cached = {"positioning_statement": "CACHED"}
    monkeypatch.setattr(
        shared.temporal, "load_checkpoint", lambda team, jid, phase: {"payload": cached}
    )
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    with patch.object(main_mod.orchestrator, "run_single_phase") as mock_rsp:
        out = activities.run_branding_phase_activity(_phase_payload(), "strategic_core", {})

    assert out == cached
    mock_rsp.assert_not_called()


def test_phase_activity_none_payload_checkpoint_does_not_short_circuit(monkeypatch) -> None:
    """A checkpoint whose payload is None must NOT short-circuit — the phase re-runs
    (guards against `existing.get("payload") is not None` treating None as cached)."""
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.models import StrategicCoreOutput
    from branding_team.temporal import activities

    monkeypatch.setattr(
        shared.temporal, "load_checkpoint", lambda team, jid, phase: {"payload": None}
    )
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    model = StrategicCoreOutput(positioning_statement="FRESH")
    with patch.object(
        main_mod.orchestrator, "run_single_phase", return_value=(model, False)
    ) as mock_rsp:
        out = activities.run_branding_phase_activity(_phase_payload(), "strategic_core", {})

    assert out["positioning_statement"] == "FRESH"
    mock_rsp.assert_called_once()


def test_market_research_activity_returns_none_on_failure() -> None:
    from branding_team.temporal import activities

    with patch(
        "branding_team.adapters.market_research.request_market_research",
        side_effect=RuntimeError("service down"),
    ):
        assert activities.run_market_research_activity(_phase_payload()) is None


def test_market_research_activity_serializes_snapshot() -> None:
    from branding_team.models import CompetitiveSnapshot
    from branding_team.temporal import activities

    snap = CompetitiveSnapshot(summary="S", similar_brands=["A"], insights=["i"])
    with patch("branding_team.adapters.market_research.request_market_research", return_value=snap):
        out = activities.run_market_research_activity(_phase_payload())

    assert out["summary"] == "S"
    assert out["similar_brands"] == ["A"]


def test_design_assets_activity_reconstructs_strategic_core() -> None:
    from branding_team.temporal import activities

    captured: dict = {}

    def _fake_request(core, company_name=""):
        from branding_team.models import DesignAssetRequestResult

        captured["positioning"] = getattr(core, "positioning_statement", None)
        captured["company_name"] = company_name
        return DesignAssetRequestResult(request_id="d1", status="pending", artifacts=[])

    with patch(
        "branding_team.adapters.design_assets.request_design_assets", side_effect=_fake_request
    ):
        out = activities.run_design_assets_activity(
            _phase_payload(), {"positioning_statement": "POS-D"}
        )

    assert out["request_id"] == "d1"
    assert captured["positioning"] == "POS-D"
    assert captured["company_name"] == "Northstar Labs"


def test_design_assets_activity_handles_missing_strategic_core() -> None:
    from branding_team.temporal import activities

    with patch("branding_team.adapters.design_assets.request_design_assets") as mock_req:
        from branding_team.models import DesignAssetRequestResult

        mock_req.return_value = DesignAssetRequestResult(
            request_id="d2", status="pending", artifacts=[]
        )
        out = activities.run_design_assets_activity(_phase_payload(), None)

    assert out["request_id"] == "d2"
    core_arg = mock_req.call_args.args[0]
    assert core_arg is None


def test_finalize_activity_completes_and_appends(monkeypatch) -> None:
    """finalize runs real compliance + assembly, persists the brand version once,
    and marks the job COMPLETED with a serialized TeamOutput."""
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.shared import job_store
    from branding_team.temporal import activities

    monkeypatch.setattr(shared.temporal, "load_checkpoint", lambda team, jid, phase: None)
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    phase_outputs = {
        "strategic_core": {
            "brand_purpose": "BP",
            "mission_statement": "MS",
            "vision_statement": "VS",
            "positioning_statement": "POS",
            "brand_promise": "PROM",
            "core_values": [],
        }
    }

    with (
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
        ) as mock_update,
        patch.object(main_mod.branding_store, "append_brand_version") as mock_append,
    ):
        activities.finalize_branding_activity(
            _phase_payload(target_phase="strategic_core"), phase_outputs, None, None
        )

    mock_append.assert_called_once()
    assert mock_append.call_args.args[:2] == ("c1", "b1")
    _, kwargs = mock_update.call_args
    assert kwargs["status"] == job_store.JOB_STATUS_COMPLETED
    # Real assembly produced a populated brand book from the strategic core.
    assert "Brand Purpose" in kwargs["result"]["brand_book"]["content"]
    assert kwargs["result"]["degraded_phases"] == []


def test_finalize_activity_unapproved_partial_run_labels_current_phase(monkeypatch) -> None:
    """The Temporal finalize path must label an unapproved partial run using
    the furthest phase actually reached (current_phase) — proving it got the
    same #3438 fix as the thread path (orchestrator.run) and that
    finalize_branding_activity's public signature is unaffected by the
    internal stop_idx removal."""
    import shared.temporal
    from branding_team.shared import job_store
    from branding_team.temporal import activities

    monkeypatch.setattr(shared.temporal, "load_checkpoint", lambda team, jid, phase: None)
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    # _model() treats a falsy (empty) dict as "phase not reached" (-> None), so
    # each present phase needs at least one field set to stay non-empty/truthy.
    phase_outputs = {
        "strategic_core": {"brand_purpose": "BP"},
        "narrative_messaging": {"tagline": "T"},
    }

    with (
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
        ) as mock_update,
        patch("branding_team.store.get_default_store"),
    ):
        activities.finalize_branding_activity(
            _phase_payload(
                target_phase="narrative_messaging",
                human_review={"approved": False, "feedback": ""},
                client_id=None,
                brand_id=None,
            ),
            phase_outputs,
            None,
            None,
        )

    _, kwargs = mock_update.call_args
    assert kwargs["status"] == job_store.JOB_STATUS_COMPLETED
    assert "Narrative Messaging" in kwargs["result"]["mission_summary"]
    assert kwargs["result"]["status"] == "needs_human_decision"


def test_finalize_activity_raises_when_append_brand_version_returns_none(monkeypatch) -> None:
    """If the brand vanished between resolve and append, finalize must fail."""
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.temporal import activities

    monkeypatch.setattr(shared.temporal, "load_checkpoint", lambda team, jid, phase: None)
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    phase_outputs = {
        "strategic_core": {
            "brand_purpose": "BP",
            "mission_statement": "MS",
            "vision_statement": "VS",
            "positioning_statement": "POS",
            "brand_promise": "PROM",
            "core_values": [],
        }
    }

    with (
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
        ) as mock_update,
        patch.object(main_mod.branding_store, "append_brand_version", return_value=None),
    ):
        from branding_team.store import BrandVersionAppendConflict

        with pytest.raises(
            BrandVersionAppendConflict,
            match="Brand row disappeared while appending brand version",
        ):
            activities.finalize_branding_activity(
                _phase_payload(target_phase="strategic_core"),
                phase_outputs,
                None,
                None,
            )

    # finalize should not write a COMPLETED row when persistence failed.
    mock_update.assert_not_called()


def test_finalize_activity_propagates_degraded_phase_checkpoint(monkeypatch) -> None:
    """A phase whose activity checkpointed degraded=True must surface in the
    finalized TeamOutput.degraded_phases, matching the thread path's signal."""
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.temporal import activities

    def _load_checkpoint(team, jid, phase):
        if phase == activities._degraded_checkpoint_key("strategic_core"):
            return {"payload": True}
        return None

    monkeypatch.setattr(shared.temporal, "load_checkpoint", _load_checkpoint)
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    phase_outputs = {
        "strategic_core": {
            "brand_purpose": "",
            "mission_statement": "",
            "vision_statement": "",
            "positioning_statement": "",
            "brand_promise": "",
            "core_values": [],
        }
    }

    with (
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
        ) as mock_update,
        patch.object(main_mod.branding_store, "append_brand_version"),
    ):
        activities.finalize_branding_activity(
            _phase_payload(target_phase="strategic_core"), phase_outputs, None, None
        )

    _, kwargs = mock_update.call_args
    assert kwargs["result"]["degraded_phases"] == ["strategic_core"]


def test_finalize_activity_finalized_checkpoint_blocks_double_append(monkeypatch) -> None:
    """A finalize retry after a prior append must not append again, but must still
    (idempotently) complete the job."""
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.shared import job_store
    from branding_team.temporal import activities

    monkeypatch.setattr(
        shared.temporal, "load_checkpoint", lambda team, jid, phase: {"payload": True}
    )
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    with (
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
        ) as mock_update,
        patch.object(main_mod.branding_store, "append_brand_version") as mock_append,
    ):
        activities.finalize_branding_activity(_phase_payload(), {}, None, None)

    mock_append.assert_not_called()
    _, kwargs = mock_update.call_args
    assert kwargs["status"] == job_store.JOB_STATUS_COMPLETED


def test_finalize_activity_skips_completion_when_cancelled(monkeypatch) -> None:
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.temporal import activities

    monkeypatch.setattr(
        shared.temporal, "load_checkpoint", lambda team, jid, phase: {"payload": True}
    )
    monkeypatch.setattr(shared.temporal, "save_checkpoint", MagicMock())

    with (
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=False
        ) as mock_update,
        patch.object(main_mod.branding_store, "append_brand_version"),
    ):
        activities.finalize_branding_activity(_phase_payload(), {}, None, None)

    # Cancelled mid-finalize is terminal: the atomic write is attempted but the
    # server-side guard reports no row updated, so there is no COMPLETED transition.
    mock_update.assert_called_once()


def test_mark_failed_activity_writes_failed_row() -> None:
    from branding_team.api import main as main_mod
    from branding_team.temporal import activities

    with patch(
        "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
    ) as mock_update:
        assert activities.mark_branding_failed_activity("job-1", "boom") is True

    _, kwargs = mock_update.call_args
    assert kwargs["status"] == main_mod.JOB_STATUS_FAILED
    assert kwargs["error"] == "boom"


def test_mark_failed_activity_skips_when_cancelled() -> None:
    from branding_team.temporal import activities

    with patch(
        "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=False
    ) as mock_update:
        assert activities.mark_branding_failed_activity("job-1", "boom") is False
        mock_update.assert_called_once()


def test_mark_failed_activity_raises_job_not_found_when_missing() -> None:
    from branding_team.shared.job_store import JobNotFoundError
    from branding_team.temporal import activities

    with patch("branding_team.shared.job_store.update_job_if_not_cancelled", return_value=None):
        with pytest.raises(JobNotFoundError):
            activities.mark_branding_failed_activity("missing-job", "boom")


def test_check_cancelled_activity_reflects_job_state() -> None:
    from branding_team.temporal import activities

    with patch("branding_team.shared.job_store.is_job_cancelled", return_value=True):
        assert activities.check_branding_cancelled_activity("job-1") is True
    with patch("branding_team.shared.job_store.is_job_cancelled", return_value=False):
        assert activities.check_branding_cancelled_activity("job-1") is False


# ---------------------------------------------------------------------------
# orchestrator.run_single_phase — isolated phase execution + context injection
# ---------------------------------------------------------------------------


def _canned_phase_result(node_id: str, json_str: str):
    agent_result = MagicMock()
    agent_result.message = {"content": [{"text": json_str}]}
    node_result = MagicMock()
    node_result.get_agent_results.return_value = [agent_result]
    result = MagicMock()
    result.result = {node_id: node_result}
    return result


def test_run_single_phase_extracts_output_and_injects_context(monkeypatch) -> None:
    from branding_team import orchestrator as orch_mod
    from branding_team.models import NarrativeMessagingOutput

    captured: dict = {}
    canned = _canned_phase_result(
        "phase2_narrative", NarrativeMessagingOutput(tagline="TAG").model_dump_json()
    )

    async def _fake_invoke(task, **kwargs):
        captured["task"] = task
        return canned

    fake_graph = MagicMock()
    fake_graph.invoke_async = AsyncMock(side_effect=_fake_invoke)
    fake_builder = MagicMock()
    fake_builder.build.return_value = fake_graph
    monkeypatch.setattr(orch_mod, "GraphBuilder", MagicMock(return_value=fake_builder))
    # Avoid constructing real Strands agents for the phase sub-graph.
    monkeypatch.setitem(
        orch_mod._PHASE_SPEC,
        BrandPhase.NARRATIVE_MESSAGING,
        orch_mod._PhaseSpec(
            builder_fn=lambda: MagicMock(),
            node_id="phase2_narrative",
            model_cls=NarrativeMessagingOutput,
        ),
    )

    mission = make_mission()
    orchestrator = orch_mod.BrandingTeamOrchestrator()
    result, degraded = orchestrator.run_single_phase(
        mission,
        BrandPhase.NARRATIVE_MESSAGING,
        {"strategic_core": {"positioning_statement": "POS-UP"}},
    )

    assert isinstance(result, NarrativeMessagingOutput)
    assert result.tagline == "TAG"
    assert degraded is False
    # The isolated phase sees the mission AND the upstream output injected into
    # its task (the context the sequential graph edge would otherwise carry).
    assert "Northstar Labs" in captured["task"]
    assert "POS-UP" in captured["task"]
    assert "strategic_core" in captured["task"]


def test_run_single_phase_uses_shared_graph_timeouts(monkeypatch) -> None:
    """Isolated phase graphs must use the same budgets as build_branding_graph."""
    from branding_team import orchestrator as orch_mod
    from branding_team.graphs.top_level import (
        DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        DEFAULT_NODE_TIMEOUT_SECONDS,
    )
    from branding_team.models import NarrativeMessagingOutput

    canned = _canned_phase_result(
        "phase2_narrative", NarrativeMessagingOutput(tagline="TAG").model_dump_json()
    )
    fake_graph = MagicMock()
    fake_graph.invoke_async = AsyncMock(return_value=canned)
    fake_builder = MagicMock()
    fake_builder.build.return_value = fake_graph
    monkeypatch.setattr(orch_mod, "GraphBuilder", MagicMock(return_value=fake_builder))
    monkeypatch.setitem(
        orch_mod._PHASE_SPEC,
        BrandPhase.NARRATIVE_MESSAGING,
        orch_mod._PhaseSpec(
            builder_fn=lambda: MagicMock(),
            node_id="phase2_narrative",
            model_cls=NarrativeMessagingOutput,
        ),
    )

    orch_mod.BrandingTeamOrchestrator().run_single_phase(
        make_mission(),
        BrandPhase.NARRATIVE_MESSAGING,
        {},
    )

    exec_arg = fake_builder.set_execution_timeout.call_args.args[0]
    node_arg = fake_builder.set_node_timeout.call_args.args[0]
    assert exec_arg is DEFAULT_EXECUTION_TIMEOUT_SECONDS
    assert node_arg is DEFAULT_NODE_TIMEOUT_SECONDS


def test_run_single_phase_first_phase_has_no_prior_context() -> None:
    mission = make_mission()
    task = orch_phase_task(mission, BrandPhase.STRATEGIC_CORE, {})
    assert "Northstar Labs" in task
    assert "upstream" not in task.lower()


def test_run_single_phase_rejects_non_runnable_phase() -> None:
    from branding_team.orchestrator import BrandingTeamOrchestrator

    with pytest.raises(ValueError, match="not a runnable"):
        BrandingTeamOrchestrator().run_single_phase(make_mission(), BrandPhase.COMPLETE, {})


def orch_phase_task(mission, phase, prior_outputs):
    from branding_team.orchestrator import BrandingTeamOrchestrator

    return BrandingTeamOrchestrator._phase_task(mission, phase, prior_outputs)


# ---------------------------------------------------------------------------
# BrandingWorkflow.run — sequencing / threading / gating / cancel / failure
# ---------------------------------------------------------------------------


def _drive_workflow(
    payload: dict,
    *,
    begin: bool = True,
    begin_error: bool = False,
    cancel_after: int | None = None,
    cancel_flag: bool = False,
    phase_error: str | None = None,
    mr_result=None,
    da_result=None,
    mr_error: bool = False,
    da_error: bool = False,
    mark_failed_error: bool = False,
    mark_failed_result: bool | None = None,
    check_cancel_error: bool = False,
) -> SimpleNamespace:
    """Run BrandingWorkflow.run with workflow.execute_activity monkeypatched.

    Args:
        payload: The workflow input payload passed to ``instance.run``.
        begin: Value returned by the fake ``begin_branding_job_activity``.
        begin_error: If True, ``begin_branding_job_activity`` raises RuntimeError
            instead of returning ``begin``.
        cancel_after: If set, the fake ``check_branding_cancelled_activity``
            returns True (cancelled) once its call count exceeds this value.
        cancel_flag: If True, calls ``instance.cancel()`` before ``run()``,
            simulating an externally-delivered cancel signal.
        phase_error: Name of the phase for which the fake
            ``run_branding_phase_activity`` raises RuntimeError instead of
            returning its canned result.
        mr_result: Value returned by the fake ``run_market_research_activity``.
        da_result: Value returned by the fake ``run_design_assets_activity``.
        mr_error: If True, ``run_market_research_activity`` raises RuntimeError
            instead of returning ``mr_result``.
        da_error: If True, ``run_design_assets_activity`` raises RuntimeError
            instead of returning ``da_result``.
        mark_failed_error: If True, ``mark_branding_failed_activity`` raises
            RuntimeError instead of returning ``mark_failed_result``.
        mark_failed_result: Value returned by the fake
            ``mark_branding_failed_activity``.
        check_cancel_error: If True, ``check_branding_cancelled_activity``
            raises RuntimeError instead of its normal cancel-check logic.

    Returns:
        A namespace of ``calls`` (one dict per execute_activity), ``prior``
        (per-phase snapshot of prior_outputs at dispatch time), ``finalize``
        (the finalize args), ``instance`` (the workflow for a post-run progress
        query), and ``error`` (the exception that escaped run()).
    """
    from branding_team.temporal import workflows as wf

    calls: list[dict] = []
    prior_snapshots: dict[str, dict] = {}
    finalize_args: dict = {}
    state = {"checks": 0}
    A = wf._activities

    async def _fake(
        activity_fn,
        *,
        args,
        task_queue=None,
        start_to_close_timeout=None,
        heartbeat_timeout=None,
        retry_policy=None,
    ):
        calls.append(
            {
                "fn": activity_fn,
                "name": activity_fn.__name__,
                "args": list(args),
                "retry": retry_policy,
                "timeout": start_to_close_timeout,
                "heartbeat": heartbeat_timeout,
            }
        )
        if activity_fn is A.begin_branding_job_activity:
            if begin_error:
                raise RuntimeError("begin-boom")
            return begin
        if activity_fn is A.check_branding_cancelled_activity:
            if check_cancel_error:
                raise RuntimeError("check-boom")
            state["checks"] += 1
            return cancel_after is not None and state["checks"] > cancel_after
        if activity_fn is A.run_branding_phase_activity:
            _payload, phase, prior = args
            prior_snapshots[phase] = dict(prior)
            if phase_error is not None and phase == phase_error:
                raise RuntimeError(f"boom-{phase}")
            return {"_out": phase}
        if activity_fn is A.run_market_research_activity:
            if mr_error:
                raise RuntimeError("mr-boom")
            return mr_result
        if activity_fn is A.run_design_assets_activity:
            if da_error:
                raise RuntimeError("da-boom")
            return da_result
        if activity_fn is A.finalize_branding_activity:
            finalize_args["payload"] = args[0]
            finalize_args["phase_outputs"] = dict(args[1])
            finalize_args["competitive_snapshot"] = args[2]
            finalize_args["design_asset_result"] = args[3]
            return None
        if activity_fn is A.mark_branding_failed_activity:
            if mark_failed_error:
                raise RuntimeError("markfailed-boom")
            return mark_failed_result
        return None

    instance = wf.BrandingWorkflow()
    if cancel_flag:
        instance.cancel()  # exercise the @workflow.signal handler
    error = None
    with patch.object(wf.workflow, "execute_activity", _fake):
        try:
            asyncio.run(instance.run(payload))
        except Exception as e:  # noqa: BLE001 — surface for assertions
            error = e
    return SimpleNamespace(
        calls=calls, prior=prior_snapshots, finalize=finalize_args, instance=instance, error=error
    )


def _names(result) -> list[str]:
    return [c["name"] for c in result.calls]


def test_workflow_runs_all_phases_in_order_then_finalizes() -> None:
    result = _drive_workflow(_phase_payload())

    names = _names(result)
    # begin, then a cancel-check + phase per phase, then finalize.
    assert names[0] == "begin_branding_job_activity"
    phase_calls = [c for c in result.calls if c["name"] == "run_branding_phase_activity"]
    phases_run = [c["args"][1] for c in phase_calls]
    assert phases_run == [
        "strategic_core",
        "narrative_messaging",
        "visual_identity",
        "channel_activation",
        "governance",
    ]
    assert names[-1] == "finalize_branding_activity"
    assert result.error is None
    assert result.instance.progress()["phase"] == "done"
    assert result.instance.progress()["fraction"] == 1.0


def test_workflow_threads_prior_outputs_forward() -> None:
    result = _drive_workflow(_phase_payload())

    # The first phase sees no prior context; each later phase sees all earlier
    # phase outputs accumulated.
    assert result.prior["strategic_core"] == {}
    assert result.prior["narrative_messaging"] == {"strategic_core": {"_out": "strategic_core"}}
    assert set(result.prior["governance"]) == {
        "strategic_core",
        "narrative_messaging",
        "visual_identity",
        "channel_activation",
    }
    # finalize receives every completed phase output.
    assert set(result.finalize["phase_outputs"]) == {
        "strategic_core",
        "narrative_messaging",
        "visual_identity",
        "channel_activation",
        "governance",
    }


def test_workflow_target_phase_truncates_and_gates_integrations() -> None:
    result = _drive_workflow(_phase_payload(target_phase="strategic_core"))

    phases_run = [c["args"][1] for c in result.calls if c["name"] == "run_branding_phase_activity"]
    assert phases_run == ["strategic_core"]
    # Integrations off by default → their activities are never dispatched.
    assert "run_market_research_activity" not in _names(result)
    assert "run_design_assets_activity" not in _names(result)
    assert "finalize_branding_activity" in _names(result)


def test_workflow_runs_enabled_integrations_and_passes_results_to_finalize() -> None:
    result = _drive_workflow(
        _phase_payload(include_market_research=True, include_design_assets=True),
        mr_result={"summary": "mr"},
        da_result={"request_id": "da"},
    )

    names = _names(result)
    assert "run_market_research_activity" in names
    assert "run_design_assets_activity" in names
    assert result.finalize["competitive_snapshot"] == {"summary": "mr"}
    assert result.finalize["design_asset_result"] == {"request_id": "da"}


def test_workflow_begin_false_is_terminal_no_finalize() -> None:
    result = _drive_workflow(_phase_payload(), begin=False)

    names = _names(result)
    assert names == ["begin_branding_job_activity"]
    assert "finalize_branding_activity" not in names
    assert "mark_branding_failed_activity" not in names
    assert result.error is None
    assert result.instance.progress()["phase"] == "cancelled"


def test_workflow_cancel_between_phases_skips_finalize() -> None:
    # cancel-check returns True on the 2nd check (before phase 2).
    result = _drive_workflow(_phase_payload(), cancel_after=1)

    phases_run = [c["args"][1] for c in result.calls if c["name"] == "run_branding_phase_activity"]
    assert phases_run == ["strategic_core"]
    names = _names(result)
    assert "finalize_branding_activity" not in names
    assert "mark_branding_failed_activity" not in names
    assert result.error is None
    assert result.instance.progress()["phase"] == "cancelled"


def test_workflow_cancel_signal_short_circuits_before_first_phase() -> None:
    result = _drive_workflow(_phase_payload(), cancel_flag=True)

    names = _names(result)
    # Signal flag is checked first (no cancel-check activity needed) and stops the
    # run before any phase.
    assert "run_branding_phase_activity" not in names
    assert "finalize_branding_activity" not in names
    assert result.instance.progress()["cancel_requested"] is True


def test_workflow_phase_failure_marks_failed_and_reraises() -> None:
    result = _drive_workflow(_phase_payload(), phase_error="strategic_core")

    names = _names(result)
    assert "mark_branding_failed_activity" in names
    assert "finalize_branding_activity" not in names
    assert isinstance(result.error, RuntimeError)


def test_workflow_uses_bounded_retry_tiers() -> None:
    result = _drive_workflow(_phase_payload())

    by_name = {}
    for c in result.calls:
        by_name.setdefault(c["name"], c)
    # LLM-tier phases retry at most twice; deterministic bookkeeping retries thrice.
    assert by_name["run_branding_phase_activity"]["retry"].maximum_attempts == 2
    assert by_name["run_branding_phase_activity"]["heartbeat"] == timedelta(minutes=5)
    assert by_name["begin_branding_job_activity"]["retry"].maximum_attempts == 3
    assert by_name["finalize_branding_activity"]["retry"].maximum_attempts == 3
    # begin/finalize/mark-failed route through the guarded-transition primitive,
    # so a missing job row (JobNotFoundError) must skip retries entirely rather
    # than burning the full attempt budget on a precondition that can't resolve.
    assert by_name["begin_branding_job_activity"]["retry"].non_retryable_error_types == [
        "JobNotFoundError"
    ]
    assert by_name["finalize_branding_activity"]["retry"].non_retryable_error_types == [
        "JobNotFoundError"
    ]


def test_workflow_mark_failed_activity_uses_non_retryable_missing_job_policy() -> None:
    result = _drive_workflow(_phase_payload(), phase_error="strategic_core")

    by_name = {c["name"]: c for c in result.calls}
    assert by_name["mark_branding_failed_activity"]["retry"].non_retryable_error_types == [
        "JobNotFoundError"
    ]


def test_workflow_cancel_race_during_mark_failed_stays_terminal_cancelled() -> None:
    """If mark_branding_failed_activity's atomic write reports False, a cancel
    landed between the workflow's own cancel-check and the write — the job row
    is cancelled, not failed, so the workflow must reclassify accordingly
    instead of raising into what would look like a failed run."""
    result = _drive_workflow(
        _phase_payload(), phase_error="strategic_core", mark_failed_result=False
    )

    assert "mark_branding_failed_activity" in _names(result)
    assert result.error is None
    assert result.instance.progress()["phase"] == "cancelled"


def test_workflow_target_phase_complete_runs_all_phases() -> None:
    """Regression: target_phase='complete' (BrandPhase.COMPLETE) must run all
    phases (like thread mode), not raise ValueError and wedge the job."""
    from branding_team.temporal.constants import PHASE_SEQUENCE

    result = _drive_workflow(_phase_payload(target_phase="complete"))

    phases_run = [c["args"][1] for c in result.calls if c["name"] == "run_branding_phase_activity"]
    assert phases_run == PHASE_SEQUENCE
    assert "finalize_branding_activity" in _names(result)
    assert result.error is None
    assert result.instance.progress()["phase"] == "done"


def test_workflow_market_research_failure_degrades_to_none() -> None:
    """A Temporal-level market-research failure is best-effort: it degrades to
    competitive_snapshot=None and the run still finalizes (matches thread mode)."""
    result = _drive_workflow(
        _phase_payload(include_market_research=True, include_design_assets=True),
        mr_error=True,
        da_result={"request_id": "da"},
    )

    assert result.error is None
    assert "finalize_branding_activity" in _names(result)
    assert result.finalize["competitive_snapshot"] is None
    # Design assets still succeeds concurrently.
    assert result.finalize["design_asset_result"] == {"request_id": "da"}


def test_workflow_design_assets_failure_fails_the_run() -> None:
    """Design-asset errors propagate (unlike market research), failing the run
    via mark_failed — matching thread mode where design-asset errors are raised."""
    result = _drive_workflow(
        _phase_payload(include_design_assets=True),
        da_error=True,
    )

    assert isinstance(result.error, RuntimeError)
    assert "mark_branding_failed_activity" in _names(result)
    assert "finalize_branding_activity" not in _names(result)


def test_workflow_mark_failed_failure_does_not_mask_original_error() -> None:
    """If mark_branding_failed_activity itself fails, the original pipeline error
    must still propagate (not be replaced by the mark-failed error)."""
    result = _drive_workflow(
        _phase_payload(),
        phase_error="strategic_core",
        mark_failed_error=True,
    )

    assert "mark_branding_failed_activity" in _names(result)
    # The escaping error is the original phase error, not 'markfailed-boom'.
    assert isinstance(result.error, RuntimeError)
    assert "boom-strategic_core" in str(result.error)


def test_workflow_begin_failure_marks_failed() -> None:
    """A begin failure (exception, not a cancel) is now inside the try, so it
    records a FAILED row instead of leaving the job silently PENDING."""
    result = _drive_workflow(_phase_payload(), begin_error=True)

    assert "mark_branding_failed_activity" in _names(result)
    assert "run_branding_phase_activity" not in _names(result)
    assert isinstance(result.error, RuntimeError)


def test_workflow_cancel_during_activity_failure_stays_terminal() -> None:
    """If the job is cancelled while a phase is failing, the run stays terminal
    (cancelled), NOT failed — the except branch re-checks cancel before re-raising,
    mirroring _run_branding_core. Otherwise Temporal would record a failed workflow
    while the job row stays cancelled."""
    # phase 1 fails; the between-phase check passed (cancel_after=1) but the
    # except-branch check then reports cancelled.
    result = _drive_workflow(_phase_payload(), phase_error="strategic_core", cancel_after=1)

    names = _names(result)
    assert "mark_branding_failed_activity" not in names  # not marked FAILED
    assert result.error is None  # workflow completes (terminal cancelled)
    assert result.instance.progress()["phase"] == "cancelled"


def test_workflow_design_assets_failure_propagates_even_with_mr_enabled() -> None:
    """Design-asset failures fail the run promptly even when market research is also
    enabled — MR is wrapped best-effort, so gather surfaces the DA error at once
    instead of waiting out MR's multi-minute timeout."""
    result = _drive_workflow(
        _phase_payload(include_market_research=True, include_design_assets=True),
        da_error=True,
        mr_result={"summary": "mr"},
    )

    assert isinstance(result.error, RuntimeError)
    assert "mark_branding_failed_activity" in _names(result)
    assert "finalize_branding_activity" not in _names(result)


def test_workflow_cancel_probe_failure_falls_through_to_failed() -> None:
    """If the cancel-probe in the failure handler itself errors, the run falls
    through to FAILED rather than hanging or masking the error."""
    result = _drive_workflow(_phase_payload(), check_cancel_error=True)

    assert "mark_branding_failed_activity" in _names(result)
    assert isinstance(result.error, RuntimeError)


# ---------------------------------------------------------------------------
# _run_branding_core / _run_branding_background (unchanged shared pipeline body)
# ---------------------------------------------------------------------------


def _core_args() -> tuple:
    from branding_team.models import HumanReview

    mission = make_mission(
        company_name="CoreCo",
        company_description="Company for core failure test",
        target_audience="users",
    )
    return ("job-core", mission, HumanReview(approved=True), [], None, None, False, False, None)


def test_run_branding_core_marks_failed_and_reraises() -> None:
    """A pipeline exception is recorded as a FAILED job row AND re-raised
    (original type/message preserved) so the Temporal activity can surface it."""
    from branding_team.api import main as main_mod

    class Boom(RuntimeError):
        pass

    with (
        patch.object(main_mod.orchestrator, "run", side_effect=Boom("kaboom")),
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled", return_value=True
        ) as mock_update,
    ):
        with pytest.raises(Boom, match="kaboom"):
            main_mod._run_branding_core(*_core_args())

    statuses = [kw.get("status") for _, kw in mock_update.call_args_list]
    assert main_mod.JOB_STATUS_FAILED in statuses


def test_run_branding_core_mark_failed_error_does_not_mask_original_exception() -> None:
    """If mark_failed itself raises (e.g. JobNotFoundError for a missing job row),
    the ORIGINAL pipeline exception must still surface — not the bookkeeping
    error — or the real cause of the failure is lost."""
    from branding_team.api import main as main_mod
    from branding_team.shared.job_store import JOB_STATUS_FAILED, JobNotFoundError

    class Boom(RuntimeError):
        pass

    def _fake_update(job_id, *, status=None, **kwargs):
        # begin_job (status=RUNNING) must succeed so the pipeline actually runs
        # and fails; only the subsequent mark_failed (status=FAILED) write raises.
        if status == JOB_STATUS_FAILED:
            raise JobNotFoundError("job missing")
        return True

    with (
        patch.object(main_mod.orchestrator, "run", side_effect=Boom("original-cause")),
        patch(
            "branding_team.shared.job_store.update_job_if_not_cancelled",
            side_effect=_fake_update,
        ),
    ):
        with pytest.raises(Boom, match="original-cause"):
            main_mod._run_branding_core(*_core_args())


def test_run_branding_background_swallows_core_failure() -> None:
    """The thread-path wrapper must never raise — the executor Future is never
    awaited, so a propagating exception would be lost/noisy."""
    from branding_team.api import main as main_mod

    with patch.object(main_mod, "_run_branding_core", side_effect=RuntimeError("boom")):
        # Must not raise.
        main_mod._run_branding_background(*_core_args())


# ---------------------------------------------------------------------------
# cancel signal wiring (best-effort, Temporal-native)
# ---------------------------------------------------------------------------


def test_signal_branding_cancel_noop_when_temporal_disabled() -> None:
    import shared.temporal
    from branding_team.api import main as main_mod

    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=False),
        patch.object(shared.temporal, "signal_workflow_sync") as mock_signal,
    ):
        main_mod._signal_branding_cancel("job-1")
        mock_signal.assert_not_called()


def test_signal_branding_cancel_sends_cancel_signal() -> None:
    import shared.temporal
    from branding_team.api import main as main_mod
    from branding_team.temporal.constants import WORKFLOW_ID_PREFIX

    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=True),
        patch.object(shared.temporal, "signal_workflow_sync") as mock_signal,
    ):
        main_mod._signal_branding_cancel("job-9")

    mock_signal.assert_called_once()
    args, _ = mock_signal.call_args
    assert args[0] == f"{WORKFLOW_ID_PREFIX}job-9"
    assert args[1] == "cancel"


def test_signal_branding_cancel_swallows_errors() -> None:
    import shared.temporal
    from branding_team.api import main as main_mod

    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=True),
        patch.object(
            shared.temporal, "signal_workflow_sync", side_effect=RuntimeError("worker down")
        ),
    ):
        # Best-effort: a signal failure must not raise (the job flag still stops it).
        main_mod._signal_branding_cancel("job-9")


# ---------------------------------------------------------------------------
# _submit_brand_run dispatch branch (integration — uses the job service)
# ---------------------------------------------------------------------------


@pytest.fixture
def branding_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """HTTP client with in-memory stores + fake job service (no Postgres/job SID).

    Temporal dispatch tests only need clients/brands present so ``/run`` can
    resolve them; they do not exercise SQL. Installing the memory doubles here
    keeps these cases self-contained when run alone with ``POSTGRES_HOST`` unset.
    The fake job client likewise avoids requiring a live ``JOB_SERVICE_URL``.
    """
    from branding_team.api import main as main_mod
    from branding_team.api.main import app
    from branding_team.shared import job_store
    from branding_team.tests._memory_stores import install_memory_stores
    from job_service_client_fake import FakeJobServiceClient

    install_memory_stores(monkeypatch)
    fake_jobs = FakeJobServiceClient(team="branding_team")
    monkeypatch.setattr(job_store, "_client", lambda: fake_jobs)
    monkeypatch.setattr(main_mod, "_job_manager", fake_jobs)
    return TestClient(app)


def _make_brand(client: TestClient) -> tuple[str, str]:
    cid = client.post("/clients", json={"name": "Temporal Client"}).json()["id"]
    bid = client.post(
        f"/clients/{cid}/brands",
        json={
            "company_name": "TempCo",
            "company_description": "Company for temporal dispatch test",
            "target_audience": "users",
        },
    ).json()["id"]
    return cid, bid


@pytest.mark.integration
def test_run_dispatches_via_temporal_when_enabled(branding_client: TestClient) -> None:
    import shared.temporal
    from branding_team.api import main as main_mod

    cid, bid = _make_brand(branding_client)
    spy = MagicMock()
    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=True),
        patch("branding_team.temporal.start_workflow.start_branding_workflow", spy),
        patch.object(main_mod._run_executor, "submit") as mock_submit,
    ):
        resp = branding_client.post(
            f"/clients/{cid}/brands/{bid}/run",
            json={"human_approved": True, "target_phase": "strategic_core"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    # Temporal path taken: workflow started, thread pool untouched.
    mock_submit.assert_not_called()
    spy.assert_called_once()
    job_id_arg, payload_arg = spy.call_args.args
    assert job_id_arg == body["job_id"]
    assert payload_arg["job_id"] == body["job_id"]
    assert payload_arg["mission"]["company_name"] == "TempCo"
    assert payload_arg["target_phase"] == "strategic_core"


@pytest.mark.integration
def test_run_temporal_dispatch_failure_returns_503_and_fails_job(
    branding_client: TestClient,
) -> None:
    import shared.temporal

    cid, bid = _make_brand(branding_client)
    with (
        patch.object(shared.temporal, "is_temporal_enabled", return_value=True),
        patch(
            "branding_team.temporal.start_workflow.start_branding_workflow",
            side_effect=RuntimeError("worker down"),
        ),
    ):
        resp = branding_client.post(
            f"/clients/{cid}/brands/{bid}/run",
            json={"human_approved": True},
        )

    assert resp.status_code == 503
    # The dispatch failure must transition the created job row to FAILED (not
    # leave it stuck PENDING). Find this brand's job in the list and check it.
    jobs = branding_client.get("/branding/jobs").json()["jobs"]
    brand_jobs = [j for j in jobs if j["brand_id"] == bid]
    assert brand_jobs, "expected a job row created for the brand"
    assert brand_jobs[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# _submit_brand_run mark_failed bookkeeping failures (unit)
# ---------------------------------------------------------------------------


def _submit_args():
    from branding_team.api.models import RunBrandRequest

    return ("client-1", "brand-1", RunBrandRequest(human_approved=True), None)


def test_submit_brand_run_temporal_mark_failed_error_still_raises_503() -> None:
    """If Temporal dispatch fails and mark_failed itself raises, still raise
    HTTPException(503) — do not leak the bookkeeping error as a 500."""
    from fastapi import HTTPException

    from branding_team.api import background as bg
    from branding_team.api import main as main_mod
    from branding_team.shared.job_store import JobNotFoundError

    brand = SimpleNamespace(mission=make_mission())
    with (
        patch.object(main_mod.branding_store, "get_brand", return_value=brand),
        patch.object(bg, "create_job"),
        patch.object(bg, "mark_failed", side_effect=JobNotFoundError("job-service unreachable")),
        patch("shared.temporal.is_temporal_enabled", return_value=True),
        patch(
            "branding_team.temporal.start_workflow.start_branding_workflow",
            side_effect=RuntimeError("worker down"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            bg._submit_brand_run(*_submit_args())

    assert exc_info.value.status_code == 503


def test_submit_brand_run_executor_shutdown_marks_failed_and_raises_503() -> None:
    from fastapi import HTTPException

    from branding_team.api import background as bg
    from branding_team.api import main as main_mod

    brand = SimpleNamespace(mission=make_mission())
    with (
        patch.object(main_mod.branding_store, "get_brand", return_value=brand),
        patch.object(bg, "create_job"),
        patch.object(bg, "mark_failed") as mock_mark_failed,
        patch("shared.temporal.is_temporal_enabled", return_value=False),
        patch.object(
            main_mod._run_executor,
            "submit",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            bg._submit_brand_run(*_submit_args())

    assert exc_info.value.status_code == 503
    mock_mark_failed.assert_called_once()
    # mark_failed(job_id, message) — assert the failure message by name, not by
    # brittle reliance on call_args.args alone without documenting which slot.
    _job_id, failure_message = mock_mark_failed.call_args.args[:2]
    assert failure_message == "run executor unavailable"
    assert _job_id  # job id is generated by the submit path


def test_submit_brand_run_executor_mark_failed_error_still_raises_503() -> None:
    """If the run executor is shut down and mark_failed itself raises, still
    raise HTTPException(503) — do not leak the bookkeeping error as a 500."""
    from fastapi import HTTPException

    from branding_team.api import background as bg
    from branding_team.api import main as main_mod
    from branding_team.shared.job_store import JobNotFoundError

    brand = SimpleNamespace(mission=make_mission())
    with (
        patch.object(main_mod.branding_store, "get_brand", return_value=brand),
        patch.object(bg, "create_job"),
        patch.object(bg, "mark_failed", side_effect=JobNotFoundError("job-service unreachable")),
        patch("shared.temporal.is_temporal_enabled", return_value=False),
        patch.object(
            main_mod._run_executor,
            "submit",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            bg._submit_brand_run(*_submit_args())

    assert exc_info.value.status_code == 503
