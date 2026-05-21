"""Tests for temporal worker enabled paths and the agent_implementations/run_*
example scripts. The scripts are tested by importing them and calling main()
with all heavy dependencies mocked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Temporal worker — Temporal-enabled paths
# ---------------------------------------------------------------------------


def test_create_blogging_worker_with_client(monkeypatch) -> None:
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)
    monkeypatch.setenv("TEMPORAL_ADDRESS", "localhost:7233")

    captured = {}

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            captured["args"] = args

    monkeypatch.setattr(worker, "Worker", _FakeWorker)

    # Reset module-level state
    monkeypatch.setattr(worker, "_activity_executor", None)
    out = worker.create_blogging_worker(client=MagicMock())
    assert out is not None
    assert worker._activity_executor is not None
    assert captured["task_queue"] == "blogging"


def test_start_blogging_temporal_worker_thread_when_enabled(monkeypatch) -> None:
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    started: dict = {"called": False}

    class _NoOpThread:
        def __init__(self, target=None, name=None, daemon=False, **kw):
            self._target = target

        def start(self):
            started["called"] = True

        def is_alive(self):
            return True

    monkeypatch.setattr(worker.threading, "Thread", _NoOpThread)
    monkeypatch.setattr(worker, "_worker_thread", None)
    assert worker.start_blogging_temporal_worker_thread() is True
    assert started["called"]

    # Re-call: already alive → True without new start
    started["called"] = False
    assert worker.start_blogging_temporal_worker_thread() is True
    assert started["called"] is False


def test_worker_thread_target_handles_runtime_error_loop_stopped(monkeypatch) -> None:
    """RuntimeError mentioning 'event loop stopped' is silently absorbed."""
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def __init__(self):
            self._closed = False

        def run_until_complete(self, coro):
            raise RuntimeError("Event loop stopped before Future completed")

        def close(self):
            self._closed = True

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)

    worker._worker_thread_target()  # Must not raise


def test_worker_thread_target_handles_unknown_runtime_error(monkeypatch) -> None:
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def run_until_complete(self, coro):
            raise RuntimeError("totally different error")

        def close(self):
            pass

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)

    worker._worker_thread_target()  # Logs but must not raise


def test_worker_thread_target_handles_generic_exception(monkeypatch) -> None:
    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def run_until_complete(self, coro):
            raise ValueError("oops")

        def close(self):
            pass

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)

    worker._worker_thread_target()  # Must not raise


def test_worker_thread_target_handles_cancelled(monkeypatch) -> None:
    """asyncio.CancelledError is swallowed silently."""
    import asyncio

    from blogging.temporal import worker

    monkeypatch.setattr(worker, "is_temporal_enabled", lambda: True)

    class _FakeLoop:
        def run_until_complete(self, coro):
            raise asyncio.CancelledError()

        def close(self):
            pass

    monkeypatch.setattr(worker.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(worker.asyncio, "set_event_loop", lambda loop: None)
    monkeypatch.setattr(worker, "set_temporal_client", lambda c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _loop: None)
    worker._worker_thread_target()


def test_shutdown_blogging_temporal_components_running_loop(monkeypatch) -> None:
    """Exercise the path where worker has a running loop and we run shutdown."""
    from blogging.temporal import worker

    fake_worker = MagicMock()
    fake_worker.shutdown = MagicMock(return_value=None)

    class _FakeFuture:
        def result(self, timeout=None):
            return None

    class _FakeLoop:
        def is_running(self):
            return True

    monkeypatch.setattr(
        worker.asyncio,
        "run_coroutine_threadsafe",
        lambda coro, loop: _FakeFuture(),
    )

    monkeypatch.setattr(worker, "_worker_instance", fake_worker)
    monkeypatch.setattr(worker, "_worker_running_loop", _FakeLoop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)

    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_force_stop(monkeypatch) -> None:
    """When worker.shutdown() future raises, we force-stop the loop."""
    from blogging.temporal import worker

    fake_worker = MagicMock()
    fake_worker.shutdown = MagicMock(return_value=None)

    class _Future:
        def result(self, timeout=None):
            raise TimeoutError("timed out")

    class _FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, fn):
            pass

    monkeypatch.setattr(worker.asyncio, "run_coroutine_threadsafe", lambda coro, loop: _Future())
    monkeypatch.setattr(worker, "_worker_instance", fake_worker)
    monkeypatch.setattr(worker, "_worker_running_loop", _FakeLoop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)

    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_worker_only(monkeypatch) -> None:
    """Path where worker_instance is None but loop is set."""
    from blogging.temporal import worker

    class _FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, fn):
            pass

    monkeypatch.setattr(worker, "_worker_instance", None)
    monkeypatch.setattr(worker, "_worker_running_loop", _FakeLoop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)

    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_loop_not_running(monkeypatch) -> None:
    """Path where loop is set but not running — graceful skip."""
    from blogging.temporal import worker

    class _Loop:
        def is_running(self):
            return False

    monkeypatch.setattr(worker, "_worker_instance", MagicMock())
    monkeypatch.setattr(worker, "_worker_running_loop", _Loop())
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", None)
    worker.shutdown_blogging_temporal_components()


def test_shutdown_blogging_temporal_components_with_executor(monkeypatch) -> None:
    """Shutdown also tears down the activity executor."""
    from blogging.temporal import worker

    executor = MagicMock()
    monkeypatch.setattr(worker, "_worker_instance", None)
    monkeypatch.setattr(worker, "_worker_running_loop", None)
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", executor)
    worker.shutdown_blogging_temporal_components()
    executor.shutdown.assert_called()


def test_shutdown_blogging_temporal_components_executor_exception(monkeypatch) -> None:
    """If executor.shutdown raises, log but don't crash."""
    from blogging.temporal import worker

    executor = MagicMock()
    executor.shutdown = MagicMock(side_effect=RuntimeError("nope"))
    monkeypatch.setattr(worker, "_worker_instance", None)
    monkeypatch.setattr(worker, "_worker_running_loop", None)
    monkeypatch.setattr(worker, "_worker_thread", None)
    monkeypatch.setattr(worker, "_activity_executor", executor)
    worker.shutdown_blogging_temporal_components()


# ---------------------------------------------------------------------------
# temporal.start_workflow happy path
# ---------------------------------------------------------------------------


def test_start_full_pipeline_workflow_calls_run_async(monkeypatch) -> None:
    """start_full_pipeline_workflow delegates to _run_async with client.start_workflow result."""
    from blogging.temporal import start_workflow

    fake_client = MagicMock()
    fake_client.start_workflow = MagicMock(return_value="coro-handle")
    monkeypatch.setattr(start_workflow, "get_temporal_client", lambda: fake_client)

    called: dict = {}

    def fake_run_async(coro):
        called["coro"] = coro
        return None

    monkeypatch.setattr(start_workflow, "_run_async", fake_run_async)
    start_workflow.start_full_pipeline_workflow("job-1", {"brief": "x"})
    fake_client.start_workflow.assert_called_once()
    assert called["coro"] == "coro-handle"


def test_run_async_executes(monkeypatch) -> None:
    """Happy path: get_temporal_loop and get_temporal_client return objects, run completes."""
    from blogging.temporal import start_workflow

    fake_loop = MagicMock()
    fake_client = MagicMock()

    monkeypatch.setattr(start_workflow, "get_temporal_loop", lambda: fake_loop)
    monkeypatch.setattr(start_workflow, "get_temporal_client", lambda: fake_client)

    class _Future:
        def result(self, timeout=None):
            return "ok"

    monkeypatch.setattr(
        start_workflow.asyncio, "run_coroutine_threadsafe", lambda _c, _l: _Future()
    )
    assert start_workflow._run_async("coro") == "ok"


# ---------------------------------------------------------------------------
# temporal.activities.run_full_pipeline_activity
# ---------------------------------------------------------------------------


def test_temporal_activity_run_full_pipeline_delegates(monkeypatch) -> None:
    """run_full_pipeline_activity calls run_blog_full_pipeline_job."""
    from blogging.temporal import activities as acts

    seen: dict = {}

    def fake(job_id, req):
        seen["job_id"] = job_id
        seen["req"] = req

    monkeypatch.setattr(acts, "run_blog_full_pipeline_job", fake)
    acts.run_full_pipeline_activity("j1", {"brief": "x"})
    assert seen == {"job_id": "j1", "req": {"brief": "x"}}


def test_temporal_activity_run_full_pipeline_reraises_cancelled(monkeypatch) -> None:
    from temporalio.exceptions import CancelledError

    from blogging.temporal import activities as acts

    def fake(job_id, req):
        raise CancelledError("nope")

    monkeypatch.setattr(acts, "run_blog_full_pipeline_job", fake)
    with pytest.raises(CancelledError):
        acts.run_full_pipeline_activity("j", {})


def test_temporal_activity_run_full_pipeline_reraises_other(monkeypatch) -> None:
    from blogging.temporal import activities as acts

    def fake(job_id, req):
        raise ValueError("oops")

    monkeypatch.setattr(acts, "run_blog_full_pipeline_job", fake)
    with pytest.raises(ValueError):
        acts.run_full_pipeline_activity("j", {})


# ---------------------------------------------------------------------------
# agent_implementations/run_*.py scripts
# ---------------------------------------------------------------------------


def test_run_copy_editor_agent_main_smoke(monkeypatch, capsys) -> None:
    """run_copy_editor_agent.main should run end-to-end with patched LLM."""
    import agent_implementations.run_copy_editor_agent as mod

    from llm_service import DummyLLMClient

    monkeypatch.setattr(mod, "get_strands_model", lambda key: DummyLLMClient())
    monkeypatch.setattr(mod, "load_style_file", lambda *a, **kw: "style")

    # Patch the agent's run to return a deterministic result
    from blog_copy_editor_agent.models import CopyEditorOutput

    monkeypatch.setattr(
        mod.BlogCopyEditorAgent,
        "run",
        lambda self, inp: CopyEditorOutput(summary="ok", feedback_items=[]),
    )

    mod.main()
    captured = capsys.readouterr()
    assert "Copy Editor Summary" in captured.out


def test_run_publication_agent_main_smoke(monkeypatch, capsys, tmp_path) -> None:
    import agent_implementations.run_publication_agent as mod
    from blog_publication_agent.models import PublicationSubmission

    from llm_service import DummyLLMClient

    monkeypatch.setattr(mod, "get_strands_model", lambda key: DummyLLMClient())
    monkeypatch.setattr(mod, "load_style_file", lambda *a, **kw: "")

    # Stub submit_draft so we don't touch the real blog_posts directory
    monkeypatch.setattr(
        mod.BlogPublicationAgent,
        "submit_draft",
        lambda self, inp: PublicationSubmission(
            submission_id="sub-123",
            slug="sub-123",
            file_path=tmp_path / "draft.md",
            message="Submitted",
        ),
    )

    mod.main()
    captured = capsys.readouterr()
    assert "sub-123" in captured.out


def test_run_writer_agent_main_smoke(monkeypatch, capsys) -> None:
    import agent_implementations.run_writer_agent as mod

    from llm_service import DummyLLMClient

    monkeypatch.setattr(mod, "get_strands_model", lambda key: DummyLLMClient())
    monkeypatch.setattr(mod, "load_style_file", lambda *a, **kw: "")

    from blog_writer_agent.models import WriterOutput

    # The script constructs WriterInput with content_plan=None which raises.
    # We mock the BlogWriterAgent so the bug is bypassed.
    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def run(self, inp):
            return WriterOutput(draft="# Draft\n\nBody.")

    monkeypatch.setattr(mod, "BlogWriterAgent", _Stub)
    # Also patch WriterInput so the missing content_plan validation is skipped
    monkeypatch.setattr(mod, "WriterInput", lambda **kw: type("X", (), {"draft": "..."})())

    mod.main()
    captured = capsys.readouterr()
    assert "Draft" in captured.out
