"""Hermetic tests for the diff endpoint."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_console.models import RunRecord, SavedInput


class _FakeStore:
    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}
        self.saved: dict[str, SavedInput] = {}

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def get_saved_input(self, saved_id):
        return self.saved.get(saved_id)


def _make_run(run_id: str, output) -> RunRecord:
    now = datetime.now(tz=timezone.utc)
    return RunRecord(
        id=run_id,
        agent_id="blogging.planner",
        team="blogging",
        saved_input_id=None,
        status="ok",
        duration_ms=1,
        trace_id=run_id,
        author="tester",
        created_at=now,
        input_data={},
        output_data=output,
        error=None,
        logs_tail=[],
        sandbox_url=None,
    )


@pytest.fixture()
def client() -> TestClient:
    import unified_api.routes.agent_console_diff as routes_mod

    fake = _FakeStore()
    routes_mod.get_store = lambda: fake  # type: ignore[assignment]

    # Seed two runs for diff testing.
    fake.runs["A" * 8] = _make_run("A" * 8, {"k": 1})
    fake.runs["B" * 8] = _make_run("B" * 8, {"k": 2})

    now = datetime.now(tz=timezone.utc)
    fake.saved["S1"] = SavedInput(
        id="S1",
        agent_id="blogging.planner",
        name="sample",
        input_data={"brief": "hi"},
        author="tester",
        description=None,
        created_at=now,
        updated_at=now,
    )

    app = FastAPI()
    app.include_router(routes_mod.router)
    return TestClient(app)


def test_identical_runs_report_identical(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/diff",
        json={
            "left": {"kind": "run", "ref": "A" * 8},
            "right": {"kind": "run", "ref": "A" * 8},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_identical"] is True
    assert body["unified_diff"] == ""


def test_different_runs_emit_unified_diff(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/diff",
        json={
            "left": {"kind": "run", "ref": "A" * 8},
            "right": {"kind": "run", "ref": "B" * 8},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_identical"] is False
    assert any(line.startswith("-") for line in body["unified_diff"].splitlines())
    assert any(line.startswith("+") for line in body["unified_diff"].splitlines())


def test_inline_vs_saved_input(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/diff",
        json={
            "left": {"kind": "saved_input", "ref": "S1"},
            "right": {"kind": "inline", "data": {"brief": "hi"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_identical"] is True


def test_unknown_run_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/diff",
        json={
            "left": {"kind": "run", "ref": "nope"},
            "right": {"kind": "inline", "data": {}},
        },
    )
    assert resp.status_code == 404


def test_inline_without_data_is_422(client: TestClient) -> None:
    resp = client.post(
        "/api/agents/diff",
        json={
            "left": {"kind": "inline"},
            "right": {"kind": "inline", "data": {}},
        },
    )
    assert resp.status_code == 422
