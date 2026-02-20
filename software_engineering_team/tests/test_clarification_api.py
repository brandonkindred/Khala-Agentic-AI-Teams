"""Tests for clarification and execution tracking API endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from software_engineering_team.api import main as api_main

app = api_main.app
execution_tracker = api_main.execution_tracker


def test_create_and_progress_clarification_session() -> None:
    client = TestClient(app)

    create = client.post(
        "/clarification/sessions",
        json={"spec_text": "Build a task tracker web app with auth."},
    )
    assert create.status_code == 200
    created = create.json()
    assert created["session_id"]
    assert created["assistant_message"]
    assert created["done_clarifying"] is False
    assert isinstance(created["open_questions"], list)

    session_id = created["session_id"]

    msg = client.post(
        f"/clarification/sessions/{session_id}/messages",
        json={"message": "Acceptance criteria: create/edit/delete tasks and role-based auth."},
    )
    assert msg.status_code == 200
    msg_payload = msg.json()
    assert msg_payload["assistant_message"]

    snapshot = client.get(f"/clarification/sessions/{session_id}")
    assert snapshot.status_code == 200
    snap = snapshot.json()
    assert snap["session_id"] == session_id
    assert len(snap["turns"]) >= 3
    assert snap["clarification_round"] >= 1


def test_execution_tasks_snapshot_contains_metrics() -> None:
    client = TestClient(app)

    execution_tracker.upsert_task("T-1", "Implement auth", "backend", ["T-0"])
    execution_tracker.start_task("T-1")
    execution_tracker.observe_loop("T-1", 1)
    execution_tracker.observe_loop("T-1", 3)
    execution_tracker.finish_task("T-1")

    resp = client.get("/execution/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert "plan_progress_percent" in data
    tasks = data["tasks"]
    assert any(t["task_id"] == "T-1" for t in tasks)
    task = next(t for t in tasks if t["task_id"] == "T-1")
    assert task["loop_count_min"] == 1
    assert task["loop_count_max"] == 3
    assert task["loop_count_avg"] == 2.0
    assert task["started_at"] is not None
    assert task["finished_at"] is not None
