"""Tests for Deepthought FastAPI endpoints.

Hits the team API which calls the real job service.  Marked integration
pending follow-up to mock the team's ``_client`` factory.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from deepthought.api.main import app

pytestmark = [pytest.mark.integration]


def _poll_deepthought(client: TestClient, job_id: str, deadline_s: float = 5.0) -> Dict[str, Any]:
    start = time.time()
    while time.time() - start < deadline_s:
        r = client.get(f"/deepthought/status/{job_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data.get("status") in {"completed", "failed", "cancelled"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Deepthought job {job_id} did not terminate in {deadline_s}s")


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_endpoint(mock_orch_cls):
    """POST /deepthought/ask submits a job; poll status for the answer."""
    from deepthought.models import AgentResult, DeepthoughtResponse

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch.process_message.return_value = DeepthoughtResponse(
        answer="The answer is 42.",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="What is the answer?",
            answer="The answer is 42.",
            confidence=0.95,
            child_results=[],
            was_decomposed=False,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )

    client = TestClient(app)
    resp = client.post(
        "/deepthought/ask",
        json={"message": "What is the answer?"},
    )

    assert resp.status_code == 200
    submission = resp.json()
    assert "job_id" in submission
    assert submission["status"] in {"pending", "running"}

    final = _poll_deepthought(client, submission["job_id"])
    assert final["status"] == "completed"
    result = final["result"]
    assert result["answer"] == "The answer is 42."
    assert result["total_agents_spawned"] == 1
    assert result["agent_tree"]["agent_name"] == "general_analyst"


def test_health_endpoint():
    """GET /health returns ok."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_with_custom_depth(mock_orch_cls):
    """POST /deepthought/ask forwards max_depth to the orchestrator."""
    from deepthought.models import AgentResult, DeepthoughtResponse

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch.process_message.return_value = DeepthoughtResponse(
        answer="Shallow answer",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="Q?",
            answer="Shallow answer",
            confidence=0.9,
            child_results=[],
            was_decomposed=False,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )

    client = TestClient(app)
    resp = client.post(
        "/deepthought/ask",
        json={"message": "Question?", "max_depth": 3},
    )

    assert resp.status_code == 200
    _poll_deepthought(client, resp.json()["job_id"])
    call_args = mock_orch.process_message.call_args
    assert call_args[0][0].max_depth == 3


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_with_decomposition_strategy(mock_orch_cls):
    """POST /deepthought/ask forwards decomposition_strategy to the orchestrator."""
    from deepthought.models import AgentResult, DeepthoughtResponse

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch.process_message.return_value = DeepthoughtResponse(
        answer="Answer",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="Q?",
            answer="Answer",
            confidence=0.9,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )

    client = TestClient(app)
    resp = client.post(
        "/deepthought/ask",
        json={"message": "Compare X and Y", "decomposition_strategy": "by_option"},
    )

    assert resp.status_code == 200
    _poll_deepthought(client, resp.json()["job_id"])
    call_args = mock_orch.process_message.call_args
    assert call_args[0][0].decomposition_strategy.value == "by_option"


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_failure_captured_in_job(mock_orch_cls):
    """When the orchestrator raises, the job ends in `failed` with error set."""
    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch.process_message.side_effect = RuntimeError("deepthought exploded")

    client = TestClient(app)
    resp = client.post("/deepthought/ask", json={"message": "fail please"})
    assert resp.status_code == 200
    final = _poll_deepthought(client, resp.json()["job_id"])
    assert final["status"] == "failed"
    assert "deepthought exploded" in (final.get("error") or "")


def test_status_404_for_unknown_job():
    client = TestClient(app)
    r = client.get("/deepthought/status/does-not-exist")
    assert r.status_code == 404


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_stream_endpoint(mock_orch_cls):
    """POST /deepthought/ask/stream returns SSE events."""
    from deepthought.models import (
        AgentEvent,
        AgentEventType,
        AgentResult,
        DeepthoughtResponse,
    )

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch

    # Simulate the orchestrator with event collection
    events_to_emit = [
        AgentEvent(
            event_type=AgentEventType.AGENT_ANALYSING,
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            detail="Analysing",
        ),
    ]

    def fake_process(request):
        return DeepthoughtResponse(
            answer="Streamed answer",
            agent_tree=AgentResult(
                agent_id="root",
                agent_name="general_analyst",
                depth=0,
                focus_question="Q?",
                answer="Streamed answer",
                confidence=0.9,
            ),
            total_agents_spawned=1,
            max_depth_reached=0,
            events=events_to_emit,
        )

    mock_orch.process_message.side_effect = fake_process
    mock_orch._collect_event = MagicMock()

    client = TestClient(app)
    resp = client.post(
        "/deepthought/ask/stream",
        json={"message": "Stream test"},
    )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    body = resp.text
    assert "event: result" in body or "event: done" in body


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_stream_starts_with_stream_open_comment(mock_orch_cls):
    """First bytes of the SSE body include `: stream open` before any event frame."""
    from deepthought.models import AgentResult, DeepthoughtResponse

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch.process_message.return_value = DeepthoughtResponse(
        answer="A",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="Q?",
            answer="A",
            confidence=0.9,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )
    mock_orch._collect_event = MagicMock()

    client = TestClient(app)
    resp = client.post("/deepthought/ask/stream", json={"message": "start"})

    assert resp.status_code == 200
    body = resp.text
    open_idx = body.find(": stream open")
    first_event_idx = body.find("event:")
    assert open_idx != -1, "expected `: stream open` comment frame"
    assert first_event_idx == -1 or open_idx < first_event_idx


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_stream_always_ends_with_done_on_error(mock_orch_cls):
    """If the orchestrator raises, the stream still emits `event: done`."""
    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch._collect_event = MagicMock()
    mock_orch.process_message.side_effect = RuntimeError("boom")

    client = TestClient(app)
    resp = client.post("/deepthought/ask/stream", json={"message": "fail please"})

    assert resp.status_code == 200
    body = resp.text
    # Error frame should appear, and the stream must terminate with event: done.
    assert "event: error" in body
    assert body.rstrip().endswith("event: done\ndata: {}") or "event: done" in body


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_stream_emits_keepalive_on_silence(mock_orch_cls, monkeypatch):
    """When the orchestrator goes silent past the keepalive interval, `: keepalive` is emitted."""
    from deepthought.models import AgentResult, DeepthoughtResponse

    # Force a very short keepalive window so the test doesn't hang.
    monkeypatch.setenv("DEEPTHOUGHT_STREAM_KEEPALIVE_SECONDS", "0.2")

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch._collect_event = MagicMock()

    def slow_process(_request):
        # Block long enough for multiple keepalive intervals to elapse.
        time.sleep(0.7)
        return DeepthoughtResponse(
            answer="slow",
            agent_tree=AgentResult(
                agent_id="root",
                agent_name="general_analyst",
                depth=0,
                focus_question="Q?",
                answer="slow",
                confidence=0.9,
            ),
            total_agents_spawned=1,
            max_depth_reached=0,
        )

    mock_orch.process_message.side_effect = slow_process

    client = TestClient(app)
    resp = client.post("/deepthought/ask/stream", json={"message": "slow"})

    # Cleanup env var regardless of assertion outcome
    os.environ.pop("DEEPTHOUGHT_STREAM_KEEPALIVE_SECONDS", None)

    assert resp.status_code == 200
    body = resp.text
    assert ": keepalive" in body
    assert "event: done" in body


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_stream_survives_malformed_keepalive_env(mock_orch_cls, monkeypatch):
    """A malformed DEEPTHOUGHT_STREAM_KEEPALIVE_SECONDS must not 500 the stream."""
    from deepthought.models import AgentResult, DeepthoughtResponse

    monkeypatch.setenv("DEEPTHOUGHT_STREAM_KEEPALIVE_SECONDS", "not-a-number")

    mock_orch = MagicMock()
    mock_orch_cls.return_value = mock_orch
    mock_orch._collect_event = MagicMock()
    mock_orch.process_message.return_value = DeepthoughtResponse(
        answer="A",
        agent_tree=AgentResult(
            agent_id="root",
            agent_name="general_analyst",
            depth=0,
            focus_question="Q?",
            answer="A",
            confidence=0.9,
        ),
        total_agents_spawned=1,
        max_depth_reached=0,
    )

    client = TestClient(app)
    resp = client.post("/deepthought/ask/stream", json={"message": "hi"})

    assert resp.status_code == 200
    assert "event: done" in resp.text
