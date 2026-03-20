"""Tests for HTTP-backed RemoteJobManager."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def remote_mgr(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JOB_SERVICE_URL", "http://job-svc")
    from remote_job_manager import RemoteJobManager

    m = RemoteJobManager(team="test_team")
    yield m
    m.close()


def test_create_job_put(remote_mgr, monkeypatch: pytest.MonkeyPatch) -> None:
    put = MagicMock()
    put.return_value.status_code = 200
    put.return_value.raise_for_status = MagicMock()
    remote_mgr._client.put = put
    remote_mgr.create_job("j1", status="pending", extra="x")
    put.assert_called()
    args, kwargs = put.call_args
    assert "/v1/jobs/test_team/j1" in args[0] or args[0].endswith("/j1")
    assert kwargs["json"]["job_id"] == "j1"


def test_apply_to_job_retries_on_409(remote_mgr, monkeypatch: pytest.MonkeyPatch) -> None:
    body: Dict[str, Any] = {"job_id": "j1", "status": "running", "n": 0}

    def fake_get(url: str, **kwargs: Any) -> MagicMock:
        r = MagicMock()
        r.status_code = 200
        r.json = lambda: dict(body)
        r.headers = {"X-Job-Version": "1"}
        r.raise_for_status = MagicMock()
        return r

    put_calls = [0]

    def fake_put(url: str, **kwargs: Any) -> MagicMock:
        put_calls[0] += 1
        r = MagicMock()
        if put_calls[0] == 1:
            r.status_code = 409
        else:
            r.status_code = 200
        r.raise_for_status = MagicMock()
        return r

    remote_mgr._client.get = fake_get
    remote_mgr._client.put = fake_put

    def bump(d: Dict[str, Any]) -> None:
        d["n"] = d.get("n", 0) + 10

    remote_mgr.apply_to_job("j1", bump)
    assert put_calls[0] == 2
