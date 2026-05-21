"""Tests for the social marketing team's Temporal client, activities,
workflows, worker, and start-workflow helpers.

These tests do not depend on a real Temporal server: ``temporalio``'s
client is monkeypatched, the workflow ``run`` body is exercised via a
worker test harness, and the worker thread entrypoint is invoked with
fakes that short-circuit network calls.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from typing import Any

import pytest

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

    async def _fake_connect(address, namespace):  # noqa: ANN001
        assert address == "temporal:7233"
        assert namespace == "social"
        return sentinel

    import temporalio.client as tc

    monkeypatch.setattr(tc.Client, "connect", staticmethod(_fake_connect))

    from social_media_marketing_team.temporal import client as cmod

    result = asyncio.run(cmod.connect_temporal_client())
    assert result is sentinel


def test_connect_temporal_client_raises_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

    async def _bad_connect(address, namespace):  # noqa: ANN001
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
# activities
# ---------------------------------------------------------------------------


def test_run_team_job_activity_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity should fetch + validate brand, then call _run_team_job."""
    from social_media_marketing_team.adapters.branding import BrandContext
    from social_media_marketing_team.api import main as api_main
    from social_media_marketing_team.temporal import activities as amod

    brand_ctx = BrandContext(
        brand_name="A",
        target_audience="t",
        voice_and_tone="v",
        brand_guidelines="g",
        brand_objectives="o",
    )

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

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch
    )
    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.validate_brand_for_social_marketing",
        _fake_validate,
    )
    monkeypatch.setattr(api_main, "_run_team_job", _fake_run_team_job)

    amod.run_team_job_activity(
        "job-1",
        {"client_id": "c", "brand_id": "b", "llm_model_name": "m"},
    )

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

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch
    )

    with pytest.raises(ApplicationError) as exc:
        amod.run_team_job_activity(
            "job-x", {"client_id": "c", "brand_id": "b", "llm_model_name": "m"}
        )
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

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch
    )
    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.validate_brand_for_social_marketing",
        _fake_validate,
    )

    with pytest.raises(ApplicationError) as exc:
        amod.run_team_job_activity(
            "job-y", {"client_id": "c", "brand_id": "b", "llm_model_name": "m"}
        )
    assert exc.value.non_retryable is True


def test_run_team_job_activity_unexpected_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    from social_media_marketing_team.temporal import activities as amod

    def _fake_fetch(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "social_media_marketing_team.adapters.branding.fetch_brand", _fake_fetch
    )

    with pytest.raises(RuntimeError):
        amod.run_team_job_activity(
            "job-z", {"client_id": "c", "brand_id": "b", "llm_model_name": "m"}
        )


# ---------------------------------------------------------------------------
# workflows.run — exercise the body without a full worker
# ---------------------------------------------------------------------------


def test_workflow_run_executes_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SocialMarketingTeamWorkflow.run`` should await execute_activity with
    the correct arguments."""
    from social_media_marketing_team.temporal import workflows as wmod

    captured: dict[str, Any] = {}

    async def _fake_execute(activity, args=None, **kwargs):  # noqa: ANN001
        captured["activity"] = activity
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(wmod.workflow, "execute_activity", _fake_execute)

    wf = wmod.SocialMarketingTeamWorkflow()
    asyncio.run(wf.run("job-1", {"k": "v"}))

    assert captured["args"] == ["job-1", {"k": "v"}]
    assert captured["kwargs"]["task_queue"] == "social-marketing"


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


def test_create_worker_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from social_media_marketing_team.temporal import worker as wmod

    assert wmod.create_social_marketing_worker(client=None) is None


def test_create_worker_returns_none_when_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    assert wmod.create_social_marketing_worker(client=None) is None


def test_create_worker_builds_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    captured: dict[str, Any] = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(wmod, "Worker", _FakeWorker)
    monkeypatch.setattr(wmod, "_activity_executor", None)

    out = wmod.create_social_marketing_worker(client=object())
    assert isinstance(out, _FakeWorker)
    assert captured["kwargs"]["task_queue"] == "social-marketing"
    assert captured["kwargs"]["max_concurrent_activities"] == 2


def test_run_worker_async_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """When connect_temporal_client returns None, _run_worker_async exits."""
    from social_media_marketing_team.temporal import worker as wmod

    async def _no_client():
        return None

    monkeypatch.setattr(wmod, "connect_temporal_client", _no_client)
    asyncio.run(wmod._run_worker_async())


def test_run_worker_async_no_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """When create_social_marketing_worker returns None, exit cleanly."""
    from social_media_marketing_team.temporal import worker as wmod

    async def _client():
        return object()

    monkeypatch.setattr(wmod, "connect_temporal_client", _client)
    monkeypatch.setattr(wmod, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(wmod, "set_temporal_loop", lambda loop: None)
    monkeypatch.setattr(wmod, "create_social_marketing_worker", lambda c: None)
    asyncio.run(wmod._run_worker_async())


def test_run_worker_async_starts_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: a worker is created and its run() is awaited."""
    from social_media_marketing_team.temporal import worker as wmod

    fake_client = object()

    async def _client():
        return fake_client

    captured: dict[str, Any] = {}

    class _Worker:
        async def run(self):
            captured["ran"] = True

    monkeypatch.setattr(wmod, "connect_temporal_client", _client)
    monkeypatch.setattr(wmod, "set_temporal_client", lambda c: captured.setdefault("client", c))
    monkeypatch.setattr(wmod, "set_temporal_loop", lambda loop: captured.setdefault("loop", loop))
    monkeypatch.setattr(wmod, "create_social_marketing_worker", lambda c: _Worker())

    asyncio.run(wmod._run_worker_async())
    assert captured["ran"] is True
    assert captured["client"] is fake_client


def test_worker_thread_target_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from social_media_marketing_team.temporal import worker as wmod

    # Should exit immediately without calling asyncio.new_event_loop
    wmod._worker_thread_target()


def test_worker_thread_target_runs_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When temporal is enabled, _worker_thread_target opens a new loop, runs the
    worker coroutine, and resets module state."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    state: dict[str, Any] = {}

    async def _fake_run_worker_async():
        state["ran"] = True

    monkeypatch.setattr(wmod, "_run_worker_async", _fake_run_worker_async)
    wmod._worker_thread_target()
    assert state["ran"] is True


def test_worker_thread_target_handles_exception(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    async def _explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(wmod, "_run_worker_async", _explode)
    with caplog.at_level("ERROR"):
        wmod._worker_thread_target()
    assert any("Temporal worker failed" in r.message for r in caplog.records)


def test_worker_thread_target_handles_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    async def _cancelled():
        raise asyncio.CancelledError

    monkeypatch.setattr(wmod, "_run_worker_async", _cancelled)
    # Should swallow CancelledError silently
    wmod._worker_thread_target()


def test_start_temporal_worker_thread_disabled_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from social_media_marketing_team.temporal import worker as wmod

    assert wmod.start_social_marketing_temporal_worker_thread() is False


def test_start_temporal_worker_thread_creates_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start path: when enabled and no live thread exists, a new thread is
    spawned and the function returns True."""
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    # Force the module-level thread slot to None to ensure new spawn
    monkeypatch.setattr(wmod, "_worker_thread", None)

    spawned: dict[str, Any] = {}

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            spawned["target"] = target
            spawned["name"] = name
            self.daemon = daemon
            self._alive = True

        def start(self):
            spawned["started"] = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(wmod.threading, "Thread", _FakeThread)
    assert wmod.start_social_marketing_temporal_worker_thread() is True
    assert spawned["started"] is True


def test_start_temporal_worker_thread_returns_true_when_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
    from social_media_marketing_team.temporal import worker as wmod

    class _AliveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(wmod, "_worker_thread", _AliveThread())
    assert wmod.start_social_marketing_temporal_worker_thread() is True
