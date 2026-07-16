"""Extra coverage for ``shared.run_pipeline_job``: heartbeat thread, SSE publish,
job-updater error swallowing, external-cancellation handling, and _fail_job.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from conftest import patch_job_event_bus_publish
from conftest import setup_artifacts_root as _setup_artifacts_root


def _make_pipeline_doubles():
    from _content_plan_test_utils import make_pipeline_doubles

    return make_pipeline_doubles()


def test_publish_terminal_swallows_publish_errors(monkeypatch) -> None:
    """A raising bus is swallowed. Patched via _import_shared — production resolves
    blogging.shared.job_event_bus first, a distinct module object from shared.job_event_bus,
    so patching only the sibling module would never reach the code under test."""
    from agents.blogging.shared import run_pipeline_job as rpj

    calls: list[str] = []

    class _BoomBus:
        @staticmethod
        def publish(*a, **kw):
            calls.append("publish")
            raise RuntimeError("publish failed")

        @staticmethod
        def cleanup_job(*a, **kw):
            calls.append("cleanup")

    monkeypatch.setattr(rpj, "_import_shared", lambda name: _BoomBus)
    rpj._publish_terminal("job-id", "complete", status="completed")  # must not raise
    assert calls == ["publish"]


def test_fail_job_works_via_shared_blog_job_store(
    monkeypatch, patched_blog_job_store_client, tmp_path: Path
) -> None:
    """_fail_job records the failure (status/error) via the shared job store."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    rpj._fail_job(
        job_id,
        "oh no",
        failed_phase="planning",
        planning_failure_reason="PARSE",
    )
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "failed"
    assert job["error"] == "oh no"


def test_publish_publishes_via_job_event_bus(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """A full run publishes at least one ``update`` event through the SSE bus."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj

    _setup_artifacts_root(monkeypatch, tmp_path)

    seen: list[tuple[str, dict]] = []

    def fake_publish(job_id, payload, event_type="update"):
        seen.append((event_type, dict(payload)))

    patch_job_event_bus_publish(monkeypatch, fake_publish)

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (ppr, draft, status),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})

    assert any(et == "update" for et, _ in seen)


def test_job_updater_swallows_publish_exception(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """A raising SSE publish is swallowed; the run still completes."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj

    _setup_artifacts_root(monkeypatch, tmp_path)

    boomed: list[str] = []

    def boom(*a, **kw):
        boomed.append("publish")
        raise RuntimeError("event bus down")

    patch_job_event_bus_publish(monkeypatch, boom)

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (ppr, draft, status),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "completed"
    assert boomed  # the raising publish was actually reached and swallowed


def test_external_cancellation_planning_path(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """A PlanningError wrapping a Temporal cancellation marks the job cancelled."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj
    from agents.blogging.shared.errors import PlanningError
    from temporalio.exceptions import CancelledError as TemporalCancelled

    _setup_artifacts_root(monkeypatch, tmp_path)

    def boom(*a, **kw):
        cancel = TemporalCancelled("cancelled")
        err = PlanningError("nope")
        err.__cause__ = cancel
        raise err

    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline", boom
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "cancelled"


def test_external_cancellation_blogging_error_path(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """A BloggingError wrapping a Temporal cancellation marks the job cancelled."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj
    from agents.blogging.shared.errors import DraftError
    from temporalio.exceptions import CancelledError as TemporalCancelled

    _setup_artifacts_root(monkeypatch, tmp_path)

    def boom(*a, **kw):
        cancel = TemporalCancelled("cancelled")
        err = DraftError("nope", iteration=1)
        err.__cause__ = cancel
        raise err

    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline", boom
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "cancelled"


def test_external_cancellation_unexpected_error_path(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """A generic error wrapping a Temporal cancellation marks the job cancelled."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj
    from temporalio.exceptions import CancelledError as TemporalCancelled

    _setup_artifacts_root(monkeypatch, tmp_path)

    def boom(*a, **kw):
        cancel = TemporalCancelled("cancelled")
        err = RuntimeError("nope")
        err.__cause__ = cancel
        raise err

    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline", boom
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})
    job = bjs.get_blog_job(job_id)
    assert job["status"] == "cancelled"


def test_mark_cancelled_swallows_update_exception(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """A failing update inside the cancellation path is swallowed (no crash)."""
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import run_pipeline_job as rpj
    from agents.blogging.shared.errors import PlanningError
    from temporalio.exceptions import CancelledError as TemporalCancelled

    _setup_artifacts_root(monkeypatch, tmp_path)

    original_update = bjs.update_blog_job
    call_count = {"n": 0}

    def angry_update(job_id, **kwargs):
        call_count["n"] += 1
        if call_count["n"] > 3 and kwargs.get("status") == "cancelled":
            raise RuntimeError("DB lost")
        return original_update(job_id, **kwargs)

    monkeypatch.setattr(bjs, "update_blog_job", angry_update)

    def boom(*a, **kw):
        cancel = TemporalCancelled("cancelled")
        err = PlanningError("nope")
        err.__cause__ = cancel
        raise err

    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline", boom
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    rpj.run_blog_full_pipeline_job(job_id, {"brief": "hi"})


def test_pipeline_heartbeat_loop_runs_body_directly(
    monkeypatch, tmp_path: Path, patched_blog_job_store_client
) -> None:
    """Drive the inner ``_pipeline_heartbeat`` body by patching threading.Event.wait."""
    from agents.blogging.shared import blog_job_store as bjs

    _setup_artifacts_root(monkeypatch, tmp_path)

    body_calls = {"n": 0}
    original_wait = threading.Event.wait

    def fake_wait(self, timeout=None):
        if timeout == 30.0:
            body_calls["n"] += 1
            if body_calls["n"] <= 1:
                return False
            return True
        return original_wait(self, timeout)

    monkeypatch.setattr(threading.Event, "wait", fake_wait)

    ppr, draft, status = _make_pipeline_doubles()
    monkeypatch.setattr(
        "agents.blogging.agent_implementations.blog_writing_process_v2.run_pipeline",
        lambda *a, **kw: (ppr, draft, status),
    )

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    from agents.blogging.shared import run_pipeline_job as rpj_run

    rpj_run.run_blog_full_pipeline_job(job_id, {"brief": "hi"})

    assert body_calls["n"] >= 1
