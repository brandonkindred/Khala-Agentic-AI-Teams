"""Tests for Deepthought FastAPI endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from deepthought.api.main import app


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_endpoint(mock_orch_cls):
    """POST /deepthought/ask returns a valid DeepthoughtResponse."""
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
    data = resp.json()
    assert data["answer"] == "The answer is 42."
    assert data["total_agents_spawned"] == 1
    assert data["agent_tree"]["agent_name"] == "general_analyst"


def test_health_endpoint():
    """GET /health returns ok."""
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_with_custom_depth(mock_orch_cls):
    """POST /deepthought/ask respects max_depth parameter."""
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
    call_args = mock_orch.process_message.call_args
    assert call_args[0][0].max_depth == 3


@patch("deepthought.api.main.DeepthoughtOrchestrator")
def test_ask_with_decomposition_strategy(mock_orch_cls):
    """POST /deepthought/ask respects decomposition_strategy parameter."""
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
    call_args = mock_orch.process_message.call_args
    assert call_args[0][0].decomposition_strategy.value == "by_option"


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
