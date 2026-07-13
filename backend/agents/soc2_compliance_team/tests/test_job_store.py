"""Tests for ``job_store.py``'s job-row read/write helpers, shared by
``api/main.py`` and ``temporal/activities.py``:

* ``_now`` timestamp helper
* ``_job_is_terminal`` / ``_update_job_terminal`` / ``_update_job_unless_terminal``
  (the terminal-status write guards)
"""

from __future__ import annotations

import pytest

from soc2_compliance_team import job_store


@pytest.fixture(autouse=True)
def _patched(monkeypatch: pytest.MonkeyPatch, fake_job_client):
    monkeypatch.setattr(job_store, "_job_manager", fake_job_client)
    return fake_job_client


# ---------------------------------------------------------------------------
# _now
# ---------------------------------------------------------------------------


def test_now_returns_iso_string() -> None:
    out = job_store._now()
    assert isinstance(out, str)
    # ISO 8601: at minimum contains "T" and ends with timezone info
    assert "T" in out
    # UTC offset: +00:00 OR 'Z'
    assert out.endswith("+00:00") or out.endswith("Z")


# ---------------------------------------------------------------------------
# _job_is_terminal / _update_job_terminal / _update_job_unless_terminal
# ---------------------------------------------------------------------------


def test_job_is_terminal_true_for_completed_and_failed(fake_job_client) -> None:
    fake_job_client.create_job("t1", status="completed")
    fake_job_client.create_job("t2", status="failed")
    assert job_store._job_is_terminal("t1") is True
    assert job_store._job_is_terminal("t2") is True


def test_job_is_terminal_false_for_pending_running_or_missing(fake_job_client) -> None:
    fake_job_client.create_job("t3", status="pending")
    fake_job_client.create_job("t4", status="running")
    assert job_store._job_is_terminal("t3") is False
    assert job_store._job_is_terminal("t4") is False
    assert job_store._job_is_terminal("does-not-exist") is False


def test_update_job_terminal_applies_when_not_terminal(fake_job_client) -> None:
    fake_job_client.create_job("g1", status="running")
    job_store._update_job_terminal("g1", status="completed", result={"status": "completed"})
    assert fake_job_client.get_job("g1")["status"] == "completed"


def test_update_job_terminal_skips_when_already_terminal(fake_job_client, caplog) -> None:
    """Whichever terminal write lands first must stick — a later one is a no-op."""
    fake_job_client.create_job("g2", status="completed", result={"status": "completed"})
    with caplog.at_level("WARNING"):
        job_store._update_job_terminal("g2", status="failed", error="too late")
    job = fake_job_client.get_job("g2")
    assert job["status"] == "completed"
    assert "error" not in job
    assert any("already terminal" in r.message for r in caplog.records)


def test_job_is_terminal_defaults_to_false_on_read_error(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    class _BoomJM:
        def get_job(self, job_id):
            raise RuntimeError("job store down")

    monkeypatch.setattr(job_store, "_job_manager", _BoomJM())
    with caplog.at_level("WARNING"):
        assert job_store._job_is_terminal("t5") is False
    assert any("Could not read job" in r.message for r in caplog.records)


def test_update_job_unless_terminal_applies_when_not_terminal(fake_job_client) -> None:
    fake_job_client.create_job("u1", status="running")
    job_store._update_job_unless_terminal("u1", current_stage="Loading repository")
    assert fake_job_client.get_job("u1")["current_stage"] == "Loading repository"


def test_update_job_unless_terminal_skips_when_already_terminal(fake_job_client, caplog) -> None:
    """A workflow that keeps running server-side after run_audit already wrote
    a terminal ``failed`` status (a lost dispatch ack) must not have its
    non-terminal ``status="running"`` write resurrect the job."""
    fake_job_client.create_job("u2", status="failed", error="dispatch timed out")
    with caplog.at_level("WARNING"):
        job_store._update_job_unless_terminal(
            "u2", status="running", current_stage="Loading repository"
        )
    job = fake_job_client.get_job("u2")
    assert job["status"] == "failed"
    assert "current_stage" not in job
    assert any("already terminal" in r.message for r in caplog.records)
