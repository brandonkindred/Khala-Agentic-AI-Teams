"""Tests for the social marketing team's Temporal client, activities,
workflows, worker, and start-workflow helpers.

These tests do not depend on a real Temporal server: ``temporalio``'s client is
monkeypatched, the workflow ``run`` body is exercised with a fake
``workflow.execute_activity``, the fine-grained stage activities are called
directly against the in-memory fake job store, and the worker starter delegates to
a monkeypatched ``shared_temporal.start_team_worker``.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future
from typing import Any

import pytest

from social_media_marketing_team.adapters.branding import BrandContext

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _req(**overrides: Any) -> dict[str, Any]:
    """Build a valid serialized ``RunMarketingTeamRequest`` for tests."""
    base: dict[str, Any] = {"client_id": "c", "brand_id": "b", "llm_model_name": "m"}
    base.update(overrides)
    return base


def _brand_ctx() -> BrandContext:
    return BrandContext(
        brand_name="Northstar",
        target_audience="growth leaders",
        voice_and_tone="clear",
        brand_guidelines="g",
        brand_objectives="o",
        messaging_pillars=["Practical education"],
    )


def _patch_brand(monkeypatch: pytest.MonkeyPatch, ctx: BrandContext | None = None) -> BrandContext:
    """Monkeypatch the branding fetch/validate to return a fixed context."""
    ctx = ctx or _brand_ctx()
    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand",
        lambda client_id, brand_id: {"raw": "data"},
    )
    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.validate_brand_for_social_marketing",
        lambda data, client_id, brand_id: ctx,
    )
    return ctx


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------


def test_constants_default_task_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_TASK_QUEUE_SOCIAL_MARKETING", raising=False)
    # Reload to pick up the env change
    import importlib

    from social_media_marketing_team.temporal import constants as cmod

    importlib.reload(cmod)
    assert cmod.TASK_QUEUE == "social-marketing"
    assert cmod.WORKFLOW_ID_PREFIX_RUN == "social-marketing-run-"


# ---------------------------------------------------------------------------
# client module
# ---------------------------------------------------------------------------


def test_get_temporal_address_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from social_media_marketing_team.temporal import client as cmod

    assert cmod.get_temporal_address() is None
    assert cmod.is_temporal_enabled() is False


def test_get_temporal_address_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "  temporal:7233 ")
    from social_media_marketing_team.temporal import client as cmod

    assert cmod.get_temporal_address() == "temporal:7233"
    assert cmod.is_temporal_enabled() is True


def test_get_temporal_namespace_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    from social_media_marketing_team.temporal import client as cmod

    assert cmod.get_temporal_namespace() == "default"


def test_get_temporal_namespace_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "  custom ")
    from social_media_marketing_team.temporal import client as cmod

    assert cmod.get_temporal_namespace() == "custom"


def test_set_and_get_temporal_client_and_loop() -> None:
    from social_media_marketing_team.temporal import client as cmod

    sentinel = object()
    cmod.set_temporal_client(sentinel)  # type: ignore[arg-type]
    assert cmod.get_temporal_client() is sentinel
    cmod.set_temporal_client(None)

    loop = asyncio.new_event_loop()
    try:
        cmod.set_temporal_loop(loop)
        assert cmod.get_temporal_loop() is loop
    finally:
        cmod.set_temporal_loop(None)
        loop.close()


def test_connect_temporal_client_returns_none_when_no_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from social_media_marketing_team.temporal import client as cmod

    result = asyncio.run(cmod.connect_temporal_client())
    assert result is None


def test_connect_temporal_client_connects_when_address_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "social")

    sentinel = object()

    async def _fake_connect(address, namespace, **kwargs):  # noqa: ANN001
        assert address == "temporal:7233"
        assert namespace == "social"
        return sentinel

    import temporalio.client as tc

    monkeypatch.setattr(tc.Client, "connect", staticmethod(_fake_connect))

    from social_media_marketing_team.temporal import client as cmod

    result = asyncio.run(cmod.connect_temporal_client())
    assert result is sentinel


def test_connect_temporal_client_raises_on_failure(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

    async def _bad_connect(address, namespace, **kwargs):  # noqa: ANN001
        raise RuntimeError("boom")

    import temporalio.client as tc

    monkeypatch.setattr(tc.Client, "connect", staticmethod(_bad_connect))

    from social_media_marketing_team.temporal import client as cmod

    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            asyncio.run(cmod.connect_temporal_client())
    assert any("Temporal client connection failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# start_workflow
# ---------------------------------------------------------------------------


def test_run_async_raises_when_no_loop_or_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team.temporal import client as cmod
    from social_media_marketing_team.temporal import start_workflow as swmod

    cmod.set_temporal_client(None)
    cmod.set_temporal_loop(None)

    async def _coro():
        return 1

    coro = _coro()
    with pytest.raises(RuntimeError):
        swmod._run_async(coro)
    coro.close()


def test_run_async_threads_through_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When loop + client are set, the coroutine is submitted to the loop
    and its result is returned."""
    from social_media_marketing_team.temporal import client as cmod
    from social_media_marketing_team.temporal import start_workflow as swmod

    cmod.set_temporal_client(object())  # type: ignore[arg-type]

    fake_loop = object()
    cmod.set_temporal_loop(fake_loop)  # type: ignore[arg-type]

    called = {}

    def _fake_threadsafe(coro, loop):
        called["coro"] = coro
        called["loop"] = loop
        f: Future = Future()
        f.set_result("done")
        return f

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _fake_threadsafe)

    async def _coro():
        return "x"

    coro = _coro()
    out = swmod._run_async(coro)
    assert out == "done"
    assert called["loop"] is fake_loop
    # Cleanup
    cmod.set_temporal_client(None)
    cmod.set_temporal_loop(None)
    coro.close()


def test_start_team_job_workflow_raises_when_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from social_media_marketing_team.temporal import client as cmod
    from social_media_marketing_team.temporal import start_workflow as swmod

    cmod.set_temporal_client(None)
    with pytest.raises(RuntimeError):
        swmod.start_team_job_workflow("job-1", {})


def test_start_team_job_workflow_invokes_run_async(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from social_media_marketing_team.temporal import client as cmod
    from social_media_marketing_team.temporal import start_workflow as swmod

    captured: dict[str, Any] = {}

    class _FakeClient:
        def start_workflow(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

            async def _noop():
                return None

            return _noop()

    cmod.set_temporal_client(_FakeClient())  # type: ignore[arg-type]

    def _fake_run_async(coro):
        captured["coro"] = coro
        # ensure the coroutine is closed to avoid RuntimeWarning
        coro.close()
        return None

    monkeypatch.setattr(swmod, "_run_async", _fake_run_async)

    swmod.start_team_job_workflow("abc", {"k": "v"})

    assert captured["kwargs"]["id"] == "social-marketing-run-abc"
    assert captured["kwargs"]["task_queue"] == "social-marketing"
    assert captured["args"][0].__name__ == "run"
    cmod.set_temporal_client(None)


# ---------------------------------------------------------------------------
# phase_models — serialization contract (enum-keyed dict round-trip)
# ---------------------------------------------------------------------------


def test_consensus_dto_roundtrips_enum_keyed_channel_mix() -> None:
    """``mode="json"`` dumps must survive a real JSON boundary and rebuild.

    ``CampaignProposal.channel_mix_strategy`` is keyed by the ``Platform`` enum;
    a plain ``model_dump()`` would leave enum-instance keys that ``json.dumps``
    rejects. The activities dump with ``mode="json"`` so keys become strings.
    """
    from social_media_marketing_team.models import CampaignProposal, Platform
    from social_media_marketing_team.temporal.phase_models import ConsensusStageResult

    proposal = CampaignProposal(
        campaign_name="c",
        objective="o",
        audience_hypothesis="a",
        channel_mix_strategy={Platform.LINKEDIN: "thought leadership"},
    )
    dto = ConsensusStageResult(
        proposal=proposal.model_dump(mode="json"),
        goals={},
        brand_name="Northstar",
        status="PASS",
    )
    # Cross an actual JSON boundary (what Temporal's default converter does).
    raw = json.loads(json.dumps(dto.model_dump()))
    rebuilt_dto = ConsensusStageResult.model_validate(raw)
    rebuilt = CampaignProposal.model_validate(rebuilt_dto.proposal)
    assert rebuilt.channel_mix_strategy[Platform.LINKEDIN] == "thought leadership"


# ---------------------------------------------------------------------------
# Pattern A exports
# ---------------------------------------------------------------------------


def test_pattern_a_exports() -> None:
    from social_media_marketing_team import temporal as t

    assert t.WORKFLOWS and t.ACTIVITIES
    assert t.SocialMarketingTeamWorkflow in t.WORKFLOWS
    names = {getattr(a, "__name__", "") for a in t.ACTIVITIES}
    assert {
        "consensus_stage_activity",
        "content_plan_stage_activity",
        "platform_stage_activity",
        "experiment_stage_activity",
        "finalize_stage_activity",
        "run_team_job_activity",
    } <= names


# ---------------------------------------------------------------------------
# fine-grained stage activities
# ---------------------------------------------------------------------------


def test_consensus_stage_activity_success(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    from social_media_marketing_team.temporal import activities as amod
    from social_media_marketing_team.temporal.phase_models import ConsensusStageResult

    ctx = _patch_brand(monkeypatch)
    fake_job_client.create_job("job-1", status="pending")

    out = amod.consensus_stage_activity("job-1", _req())
    dto = ConsensusStageResult.model_validate(out)

    assert dto.status == "PASS"
    assert dto.brand_name == ctx.brand_name
    assert dto.proposal["campaign_name"].startswith(ctx.brand_name)
    assert dto.goals["brand_name"] == ctx.brand_name
    # Progress advanced to 30
    assert fake_job_client.get_job("job-1")["progress"] == 30


def test_consensus_stage_activity_brand_error_non_retryable(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from temporalio.exceptions import ApplicationError

    from social_media_marketing_team.adapters.branding import BrandNotFoundError
    from social_media_marketing_team.temporal import activities as amod

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand",
        lambda client_id, brand_id: (_ for _ in ()).throw(BrandNotFoundError("c", "b")),
    )
    fake_job_client.create_job("job-brand", status="pending")

    with pytest.raises(ApplicationError) as exc:
        amod.consensus_stage_activity("job-brand", _req())
    assert exc.value.non_retryable is True
    # Job was marked failed before the non-retryable raise.
    assert fake_job_client.get_job("job-brand")["status"] == "failed"


def test_consensus_stage_activity_unexpected_brand_error_marks_failed_and_reraises(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """An unexpected (retryable) brand-fetch error marks the job failed and re-raises.

    Unlike a missing/incomplete brand (non-retryable), a network/timeout fault is
    retryable, but the job store must still reflect the failure rather than sit in
    ``running`` until the stale monitor fires.
    """
    from social_media_marketing_team.temporal import activities as amod

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand",
        lambda client_id, brand_id: (_ for _ in ()).throw(RuntimeError("branding API 502")),
    )
    fake_job_client.create_job("job-brand-net", status="pending")

    with pytest.raises(RuntimeError):
        amod.consensus_stage_activity("job-brand-net", _req())
    assert fake_job_client.get_job("job-brand-net")["status"] == "failed"


def test_consensus_stage_activity_cancelled_brand_fetch_maps_to_cancelled(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """A cancellation during brand fetch is surfaced as cancelled, not failed."""
    from temporalio.exceptions import CancelledError

    from social_media_marketing_team.temporal import activities as amod

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand",
        lambda client_id, brand_id: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    monkeypatch.setattr(amod, "_is_cancelled", lambda: True)
    fake_job_client.create_job("job-brand-cx", status="pending")

    with pytest.raises(CancelledError):
        amod.consensus_stage_activity("job-brand-cx", _req())
    assert fake_job_client.get_job("job-brand-cx")["status"] == "cancelled"


def test_consensus_stage_activity_body_error_returns_fail_dto(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    monkeypatch.setattr(
        SocialMediaMarketingOrchestrator,
        "build_consensus_proposal",
        lambda self, goals: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    fake_job_client.create_job("job-fail", status="pending")

    out = amod.consensus_stage_activity("job-fail", _req())
    assert out["status"] == "FAIL"
    assert fake_job_client.get_job("job-fail")["status"] == "failed"


def test_run_stage_reraises_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Temporal-native cancellation propagates out of the funnel (never a FAIL DTO)."""
    from temporalio.exceptions import CancelledError

    from social_media_marketing_team.temporal import activities as amod

    def _body():
        raise CancelledError("cancelled")

    with pytest.raises(CancelledError):
        amod._run_stage("job-c", "content_plan", lambda: {"status": "FAIL"}, _body)


def test_run_stage_cancelled_body_error_maps_to_cancellation(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """A body error while the activity is cancelled becomes a cancellation, not a FAIL.

    Sync activities don't heartbeat, so cancellation surfaces via
    ``activity.is_cancelled()`` rather than a raised ``CancelledError``; the funnel
    must still mark the job cancelled and propagate a ``CancelledError``.
    """
    from temporalio.exceptions import CancelledError

    from social_media_marketing_team.temporal import activities as amod

    fake_job_client.create_job("job-cx", status="running")
    monkeypatch.setattr(amod, "_is_cancelled", lambda: True)

    def _body():
        raise RuntimeError("boom")

    with pytest.raises(CancelledError):
        amod._run_stage("job-cx", "content_plan", lambda: {"status": "FAIL"}, _body)
    assert fake_job_client.get_job("job-cx")["status"] == "cancelled"


def test_is_last_attempt_true_outside_activity_context() -> None:
    from social_media_marketing_team.temporal import activities as amod

    # No activity context -> treat as last attempt so the caller marks terminal.
    assert amod._is_last_attempt() is True


def test_is_cancelled_false_outside_activity_context() -> None:
    from social_media_marketing_team.temporal import activities as amod

    assert amod._is_cancelled() is False


def test_is_last_attempt_reads_scheduled_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from temporalio.common import RetryPolicy

    from social_media_marketing_team.temporal import activities as amod

    def _info(retry_policy, attempt):
        return type("I", (), {"retry_policy": retry_policy, "attempt": attempt})()

    monkeypatch.setattr(amod.activity, "info", lambda: _info(RetryPolicy(maximum_attempts=3), 3))
    assert amod._is_last_attempt() is True

    monkeypatch.setattr(amod.activity, "info", lambda: _info(RetryPolicy(maximum_attempts=3), 1))
    assert amod._is_last_attempt() is False

    # maximum_attempts <= 0 means unlimited retries -> never the last attempt.
    monkeypatch.setattr(amod.activity, "info", lambda: _info(RetryPolicy(maximum_attempts=0), 9))
    assert amod._is_last_attempt() is False


def test_is_cancelled_reads_activity_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team.temporal import activities as amod

    monkeypatch.setattr(amod.activity, "is_cancelled", lambda: True)
    assert amod._is_cancelled() is True


def test_content_plan_stage_activity_success(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from social_media_marketing_team.temporal import activities as amod
    from social_media_marketing_team.temporal.phase_models import ContentPlanStageResult

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-2", status="running")
    consensus = amod.consensus_stage_activity("job-2", _req())

    out = amod.content_plan_stage_activity("job-2", _req(), consensus)
    dto = ContentPlanStageResult.model_validate(out)

    assert dto.status == "PASS"
    assert dto.content_plan["campaign_name"] == consensus["proposal"]["campaign_name"]
    assert fake_job_client.get_job("job-2")["progress"] == 60


def test_content_plan_stage_activity_body_error_returns_fail_dto(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from social_media_marketing_team.orchestrator import SocialMediaMarketingOrchestrator
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-2b", status="running")
    consensus = amod.consensus_stage_activity("job-2b", _req())

    monkeypatch.setattr(
        SocialMediaMarketingOrchestrator,
        "_plan_content",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = amod.content_plan_stage_activity("job-2b", _req(), consensus)
    assert out["status"] == "FAIL"
    assert fake_job_client.get_job("job-2b")["status"] == "failed"


def test_platform_stage_activity_success(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    from social_media_marketing_team.temporal import activities as amod
    from social_media_marketing_team.temporal.phase_models import PlatformStageResult

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-3", status="running")
    consensus = amod.consensus_stage_activity("job-3", _req())
    content = amod.content_plan_stage_activity("job-3", _req(), consensus)

    out = amod.platform_stage_activity("job-3", _req(), consensus, content)
    dto = PlatformStageResult.model_validate(out)

    assert dto.status == "PASS"
    # One plan per platform specialist (LinkedIn, Facebook, Instagram, X).
    assert len(dto.platform_execution_plans) == 4


def test_experiment_stage_activity_success(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from social_media_marketing_team.temporal import activities as amod
    from social_media_marketing_team.temporal.phase_models import ExperimentStageResult

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-4", status="running")
    consensus = amod.consensus_stage_activity("job-4", _req())
    content = amod.content_plan_stage_activity("job-4", _req(), consensus)

    out = amod.experiment_stage_activity("job-4", _req(), consensus, content)
    dto = ExperimentStageResult.model_validate(out)

    assert dto.status == "PASS"
    assert dto.experiment_plan is not None
    assert dto.experiment_plan["campaign_name"] == consensus["proposal"]["campaign_name"]


def test_finalize_stage_activity_approved(monkeypatch: pytest.MonkeyPatch, fake_job_client) -> None:
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-5", status="running")
    req = _req(human_approved_for_testing=True)
    consensus = amod.consensus_stage_activity("job-5", req)
    content = amod.content_plan_stage_activity("job-5", req, consensus)
    platform = amod.platform_stage_activity("job-5", req, consensus, content)
    experiment = amod.experiment_stage_activity("job-5", req, consensus, content)

    amod.finalize_stage_activity("job-5", req, consensus, True, content, platform, experiment)

    job = fake_job_client.get_job("job-5")
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["result"]["status"] == "approved_for_testing"
    assert job["result"]["experiment_plan"] is not None
    assert len(job["result"]["platform_execution_plans"]) == 4


def test_finalize_stage_activity_needs_revision(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-6", status="running")
    req = _req(human_approved_for_testing=False)
    consensus = amod.consensus_stage_activity("job-6", req)

    # Unapproved path: only consensus ran; finalize produces NEEDS_REVISION.
    amod.finalize_stage_activity("job-6", req, consensus, False)

    job = fake_job_client.get_job("job-6")
    assert job["status"] == "completed"
    assert job["result"]["status"] == "needs_revision"
    assert job["result"]["content_plan"] is None


def test_finalize_stage_activity_approved_missing_content_non_retryable(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """Approved finalize with no content DTO raises a non-retryable ApplicationError."""
    from temporalio.exceptions import ApplicationError

    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-6b", status="running")
    req = _req(human_approved_for_testing=True)
    consensus = amod.consensus_stage_activity("job-6b", req)

    with pytest.raises(ApplicationError) as exc:
        amod.finalize_stage_activity("job-6b", req, consensus, True, content=None)
    assert exc.value.non_retryable is True


def test_finalize_stage_activity_store_failure_last_attempt_marks_failed(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """On the final attempt, a completion-store failure marks the job failed + re-raises."""
    from social_media_marketing_team.api import main as api_main
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-7", status="running")
    req = _req(human_approved_for_testing=True)
    consensus = amod.consensus_stage_activity("job-7", req)
    content = amod.content_plan_stage_activity("job-7", req, consensus)
    platform = amod.platform_stage_activity("job-7", req, consensus, content)
    experiment = amod.experiment_stage_activity("job-7", req, consensus, content)

    # Fail the terminal completion write; this is the last retry attempt.
    def _raise_on_complete(job_id, **fields):
        if fields.get("status") == "completed":
            raise RuntimeError("store down")

    marked: dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_update_job", _raise_on_complete)
    monkeypatch.setattr(amod, "_is_last_attempt", lambda: True)
    monkeypatch.setattr(
        amod, "_fail_activity", lambda job_id, exc, phase: marked.update(job=job_id, phase=phase)
    )

    with pytest.raises(RuntimeError):
        amod.finalize_stage_activity("job-7", req, consensus, True, content, platform, experiment)
    assert marked == {"job": "job-7", "phase": "finalize"}


def test_finalize_stage_activity_store_failure_not_last_attempt_reraises(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """Before the final attempt, a store failure re-raises for Temporal to retry (no fail-mark)."""
    from social_media_marketing_team.api import main as api_main
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job("job-7b", status="running")
    req = _req(human_approved_for_testing=True)
    consensus = amod.consensus_stage_activity("job-7b", req)
    content = amod.content_plan_stage_activity("job-7b", req, consensus)
    platform = amod.platform_stage_activity("job-7b", req, consensus, content)
    experiment = amod.experiment_stage_activity("job-7b", req, consensus, content)

    def _raise_on_complete(job_id, **fields):
        if fields.get("status") == "completed":
            raise RuntimeError("store blip")

    fail_called: dict[str, Any] = {}
    monkeypatch.setattr(api_main, "_update_job", _raise_on_complete)
    monkeypatch.setattr(amod, "_is_last_attempt", lambda: False)
    monkeypatch.setattr(amod, "_fail_activity", lambda *a, **k: fail_called.setdefault("hit", True))

    with pytest.raises(RuntimeError):
        amod.finalize_stage_activity("job-7b", req, consensus, True, content, platform, experiment)
    assert "hit" not in fail_called  # not marked failed while retries remain


def _finalize_with_completion_error(monkeypatch, fake_job_client, job_id, exc):
    """Build an approved finalize whose completion write raises ``exc``."""
    from social_media_marketing_team.api import main as api_main
    from social_media_marketing_team.temporal import activities as amod

    _patch_brand(monkeypatch)
    fake_job_client.create_job(job_id, status="running")
    req = _req(human_approved_for_testing=True)
    consensus = amod.consensus_stage_activity(job_id, req)
    content = amod.content_plan_stage_activity(job_id, req, consensus)
    platform = amod.platform_stage_activity(job_id, req, consensus, content)
    experiment = amod.experiment_stage_activity(job_id, req, consensus, content)

    def _update(job, **fields):
        if fields.get("status") == "completed":
            raise exc
        fake_job_client.update_job(job, **fields)  # let _mark_cancelled through

    monkeypatch.setattr(api_main, "_update_job", _update)
    return amod, req, consensus, content, platform, experiment


def test_finalize_cancellederror_maps_to_cancelled(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from temporalio.exceptions import CancelledError

    amod, req, consensus, content, platform, experiment = _finalize_with_completion_error(
        monkeypatch, fake_job_client, "job-8", CancelledError("cancelled")
    )
    with pytest.raises(CancelledError):
        amod.finalize_stage_activity("job-8", req, consensus, True, content, platform, experiment)
    assert fake_job_client.get_job("job-8")["status"] == "cancelled"


def test_finalize_store_error_while_cancelled_maps_to_cancelled(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    from temporalio.exceptions import CancelledError

    amod, req, consensus, content, platform, experiment = _finalize_with_completion_error(
        monkeypatch, fake_job_client, "job-8b", RuntimeError("boom")
    )
    monkeypatch.setattr(amod, "_is_cancelled", lambda: True)
    with pytest.raises(CancelledError):
        amod.finalize_stage_activity("job-8b", req, consensus, True, content, platform, experiment)
    assert fake_job_client.get_job("job-8b")["status"] == "cancelled"


# ---------------------------------------------------------------------------
# legacy whole-pipeline activity (kept registered for drain-out)
# ---------------------------------------------------------------------------


def test_run_team_job_activity_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity should fetch + validate brand, then call _run_team_job."""
    from social_media_marketing_team.api import main as api_main
    from social_media_marketing_team.temporal import activities as amod

    brand_ctx = _brand_ctx()
    fetched: dict[str, Any] = {}

    def _fake_fetch(client_id, brand_id):
        fetched["client_id"] = client_id
        fetched["brand_id"] = brand_id
        return {"raw": "data"}

    def _fake_validate(data, client_id, brand_id):
        fetched["validated"] = True
        return brand_ctx

    captured: dict[str, Any] = {}

    def _fake_run_team_job(job_id, request, ctx):
        captured["job_id"] = job_id
        captured["client_id"] = request.client_id
        captured["ctx"] = ctx

    monkeypatch.setattr("social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch)
    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.validate_brand_for_social_marketing",
        _fake_validate,
    )
    monkeypatch.setattr(api_main, "_run_team_job", _fake_run_team_job)

    amod.run_team_job_activity("job-1", _req())

    assert fetched["validated"] is True
    assert captured["job_id"] == "job-1"
    assert captured["ctx"] is brand_ctx


def test_run_team_job_activity_brand_not_found_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from temporalio.exceptions import ApplicationError

    from social_media_marketing_team.adapters.branding import BrandNotFoundError
    from social_media_marketing_team.temporal import activities as amod

    def _fake_fetch(*a, **k):
        raise BrandNotFoundError("c", "b")

    monkeypatch.setattr("social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch)

    with pytest.raises(ApplicationError) as exc:
        amod.run_team_job_activity("job-x", _req())
    assert exc.value.non_retryable is True


def test_run_team_job_activity_brand_incomplete_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from temporalio.exceptions import ApplicationError

    from social_media_marketing_team.adapters.branding import (
        BrandIncompleteError,
    )
    from social_media_marketing_team.temporal import activities as amod

    def _fake_fetch(*a, **k):
        return {"latest_output": {}}

    def _fake_validate(*a, **k):
        raise BrandIncompleteError("c", "b", ["strategic_core"], "draft")

    monkeypatch.setattr("social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch)
    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.validate_brand_for_social_marketing",
        _fake_validate,
    )

    with pytest.raises(ApplicationError) as exc:
        amod.run_team_job_activity("job-y", _req())
    assert exc.value.non_retryable is True


def test_run_team_job_activity_unexpected_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team.temporal import activities as amod

    def _fake_fetch(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch)

    with pytest.raises(RuntimeError):
        amod.run_team_job_activity("job-z", _req())


# ---------------------------------------------------------------------------
# workflow — sequence the stage activities (fake execute_activity)
# ---------------------------------------------------------------------------


def _fake_workflow(monkeypatch, *, patched: bool, results: dict[str, dict] | None = None):
    """Patch ``workflow.patched`` + ``workflow.execute_activity``; record order.

    Returns the ``order`` list that records each scheduled activity name.
    """
    from social_media_marketing_team.temporal import workflows as wmod

    results = results or {}
    order: list[str] = []

    async def _fake_execute(activity, args=None, **kwargs):  # noqa: ANN001
        name = getattr(activity, "__name__", str(activity))
        order.append(name)
        return results.get(name, {"status": "PASS"})

    monkeypatch.setattr(wmod.workflow, "patched", lambda name: patched)
    monkeypatch.setattr(wmod.workflow, "execute_activity", _fake_execute)
    return wmod, order


def test_workflow_approved_runs_all_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    wmod, order = _fake_workflow(monkeypatch, patched=True)

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req(human_approved_for_testing=True)))

    assert order == [
        "consensus_stage_activity",
        "content_plan_stage_activity",
        "platform_stage_activity",
        "experiment_stage_activity",
        "finalize_stage_activity",
    ]


def test_workflow_unapproved_runs_consensus_then_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wmod, order = _fake_workflow(monkeypatch, patched=True)

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req(human_approved_for_testing=False)))

    assert order == ["consensus_stage_activity", "finalize_stage_activity"]


def test_workflow_consensus_fail_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    wmod, order = _fake_workflow(
        monkeypatch,
        patched=True,
        results={"consensus_stage_activity": {"status": "FAIL"}},
    )

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req(human_approved_for_testing=True)))

    assert order == ["consensus_stage_activity"]


def test_workflow_content_fail_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    wmod, order = _fake_workflow(
        monkeypatch,
        patched=True,
        results={"content_plan_stage_activity": {"status": "FAIL"}},
    )

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req(human_approved_for_testing=True)))

    assert order == ["consensus_stage_activity", "content_plan_stage_activity"]


def test_workflow_platform_fail_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    wmod, order = _fake_workflow(
        monkeypatch,
        patched=True,
        results={"platform_stage_activity": {"status": "FAIL"}},
    )

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req(human_approved_for_testing=True)))

    assert order == [
        "consensus_stage_activity",
        "content_plan_stage_activity",
        "platform_stage_activity",
    ]


def test_workflow_experiment_fail_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    wmod, order = _fake_workflow(
        monkeypatch,
        patched=True,
        results={"experiment_stage_activity": {"status": "FAIL"}},
    )

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req(human_approved_for_testing=True)))

    assert order == [
        "consensus_stage_activity",
        "content_plan_stage_activity",
        "platform_stage_activity",
        "experiment_stage_activity",
    ]


def test_workflow_drain_out_branch_runs_legacy_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unpatched replay branch re-schedules the legacy whole-pipeline activity."""
    wmod, order = _fake_workflow(monkeypatch, patched=False)

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", _req()))

    assert order == ["run_team_job_activity"]


# ---------------------------------------------------------------------------
# worker — delegates to shared_temporal.start_team_worker
# ---------------------------------------------------------------------------


def test_start_temporal_worker_thread_disabled_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from social_media_marketing_team.temporal import worker as wmod

    called: dict[str, Any] = {}
    monkeypatch.setattr(wmod, "start_team_worker", lambda *a, **k: called.setdefault("hit", True))

    assert wmod.start_social_marketing_temporal_worker_thread() is False
    assert "hit" not in called  # never delegated when disabled


def test_start_temporal_worker_thread_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import ACTIVITIES, WORKFLOWS
    from social_media_marketing_team.temporal import worker as wmod

    captured: dict[str, Any] = {}

    def _fake_start(team, workflows, activities, task_queue=None):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(wmod, "start_team_worker", _fake_start)

    assert wmod.start_social_marketing_temporal_worker_thread() is True
    assert captured["team"] == "social_marketing"
    assert captured["task_queue"] == "social-marketing"
    assert captured["workflows"] is WORKFLOWS
    assert captured["activities"] is ACTIVITIES


# ---------------------------------------------------------------------------
# API lifespan backstop — starts scheduler + worker
# ---------------------------------------------------------------------------


def test_api_startup_starts_scheduler_and_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team.api import main as api_main

    calls: list[str] = []
    monkeypatch.setattr(api_main, "start_scheduler", lambda: calls.append("scheduler"))
    monkeypatch.setattr(
        "social_media_marketing_team.temporal.worker.start_social_marketing_temporal_worker_thread",
        lambda: calls.append("worker"),
    )

    api_main._startup()
    assert calls == ["scheduler", "worker"]


def test_api_startup_worker_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team.api import main as api_main

    calls: list[str] = []
    monkeypatch.setattr(api_main, "start_scheduler", lambda: calls.append("scheduler"))

    def _boom():
        raise RuntimeError("no temporal")

    monkeypatch.setattr(
        "social_media_marketing_team.temporal.worker.start_social_marketing_temporal_worker_thread",
        _boom,
    )

    # Should not raise — the worker start is best-effort.
    api_main._startup()
    assert calls == ["scheduler"]
