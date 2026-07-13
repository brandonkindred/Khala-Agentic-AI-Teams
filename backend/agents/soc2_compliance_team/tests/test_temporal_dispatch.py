"""Tests for the SOC2 ``run_audit`` dispatch branch (Temporal vs thread mode).

Patches ``_is_temporal_enabled`` to drive both branches without a Temporal
server, and verifies a dispatch failure marks the job failed rather than leaving
it orphaned in ``pending``.
"""

import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from soc2_compliance_team import job_store
from soc2_compliance_team.api import main as api_main
from soc2_compliance_team.api.main import app

client = TestClient(app)


def test_is_temporal_enabled_false_on_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``shared_temporal`` can't provide ``is_temporal_enabled``, default to
    thread mode rather than raising."""
    fake = types.ModuleType("shared_temporal")  # lacks is_temporal_enabled
    monkeypatch.setitem(sys.modules, "shared_temporal", fake)
    assert api_main._is_temporal_enabled() is False


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    monkeypatch.setattr(job_store, "_job_manager", fake_job_client)
    return fake_job_client


def test_dispatch_uses_temporal_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(api_main, "_is_temporal_enabled", lambda: True)

    started: dict = {}

    def _fake_start(job_id, repo_path):
        started["job_id"] = job_id
        started["repo_path"] = repo_path

    monkeypatch.setattr(
        "soc2_compliance_team.temporal.start_workflow.start_audit_workflow", _fake_start
    )
    # The Temporal branch must not spawn a thread.
    monkeypatch.setattr(
        api_main.threading, "Thread", lambda *a, **k: pytest.fail("thread spawned in Temporal mode")
    )

    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 200
    assert "Temporal" in r.json()["message"]
    assert started["job_id"] == r.json()["job_id"]


def test_dispatch_uses_thread_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(api_main, "_is_temporal_enabled", lambda: False)

    def _fail(*a, **k):
        pytest.fail("start_audit_workflow called in thread mode")

    monkeypatch.setattr("soc2_compliance_team.temporal.start_workflow.start_audit_workflow", _fail)

    spawned: dict = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), **k):
            spawned["target"] = target
            spawned["args"] = args

        def start(self):
            spawned["started"] = True

        daemon = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 200
    assert spawned["started"] is True
    assert spawned["target"] is api_main._run_audit_job


def test_dispatch_marks_failed_on_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(api_main, "_is_temporal_enabled", lambda: True)

    def _boom(job_id, repo_path):
        raise RuntimeError("no worker client")

    monkeypatch.setattr("soc2_compliance_team.temporal.start_workflow.start_audit_workflow", _boom)

    updates: list = []
    monkeypatch.setattr(job_store, "_update_job", lambda job_id, **fields: updates.append(fields))

    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 503
    # The job row must be marked failed, not left pending.
    assert any(u.get("status") == "failed" for u in updates)


def test_dispatch_503_survives_job_store_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If marking the job failed also errors, the client still gets the 503."""
    (tmp_path / "f.txt").write_text("x")
    monkeypatch.setattr(api_main, "_is_temporal_enabled", lambda: True)

    def _boom(job_id, repo_path):
        raise RuntimeError("no worker client")

    monkeypatch.setattr("soc2_compliance_team.temporal.start_workflow.start_audit_workflow", _boom)

    def _update_boom(job_id, **fields):
        raise RuntimeError("job store down")

    monkeypatch.setattr(job_store, "_update_job", _update_boom)

    r = client.post("/soc2-audit/run", json={"repo_path": str(tmp_path)})
    assert r.status_code == 503
