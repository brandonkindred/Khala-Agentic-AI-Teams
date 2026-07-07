"""Tests for the SE-side human-in-the-loop helpers: the planning decision gate, the escalating
Planning answer callback, and the adapter carrying questions across the handoff."""

from __future__ import annotations

from typing import Any, Dict

from software_engineering_team import orchestrator as se_orch
from software_engineering_team.planning_adapter import adapt_planning_result


def _wire_answer(monkeypatch, job: Dict[str, Any], option: str = "yes"):
    """Patch the SE pause primitives so the gate 'pauses' then immediately receives answers."""
    store: Dict[str, Any] = {}
    monkeypatch.setattr(se_orch, "add_pending_questions", lambda jid, qs: store.update({"qs": qs}))
    monkeypatch.setattr(se_orch, "slack_notify_open_questions", None)
    monkeypatch.setattr(se_orch, "get_job", lambda jid: job)

    def wait(jid):
        job["submitted_answers"] = [
            {"question_id": q["id"], "selected_option_id": option} for q in store.get("qs", [])
        ]
        return True

    monkeypatch.setattr(se_orch, "_wait_for_user_answers", wait)
    return store


def test_se_decision_gate_success(monkeypatch):
    job: Dict[str, Any] = {"submitted_answers": []}
    _wire_answer(monkeypatch, job)
    resolved, ok = se_orch._run_se_decision_gate(
        "j", ["Allergen strictness default?"], source="planning"
    )
    assert ok is True
    assert resolved[0]["question_text"] == "Allergen strictness default?"
    assert resolved[0]["selected_option_id"] == "yes"


def test_se_decision_gate_no_questions():
    assert se_orch._run_se_decision_gate("j", [], "planning") == ([], True)


def test_se_decision_gate_no_answers(monkeypatch):
    monkeypatch.setattr(se_orch, "add_pending_questions", lambda jid, qs: None)
    monkeypatch.setattr(se_orch, "slack_notify_open_questions", None)
    monkeypatch.setattr(se_orch, "_wait_for_user_answers", lambda jid: False)
    assert se_orch._run_se_decision_gate("j", ["Q?"], "planning") == ([], False)


def test_planning_answer_callback_escalates_and_preserves_ids(monkeypatch):
    job: Dict[str, Any] = {"submitted_answers": []}
    store = _wire_answer(monkeypatch, job)
    cb = se_orch._build_planning_answer_callback("j")
    answers = cb(
        [{"id": "pra1", "question_text": "Q?", "options": [{"id": "yes", "label": "Yes"}]}]
    )
    assert answers[0]["question_id"] == "pra1"
    # The structured question preserved the PRA question id and options.
    assert store["qs"][0]["id"] == "pra1"
    assert store["qs"][0]["options"] == [{"id": "yes", "label": "Yes"}]


def test_planning_answer_callback_returns_empty_without_answers(monkeypatch):
    monkeypatch.setattr(se_orch, "add_pending_questions", lambda jid, qs: None)
    monkeypatch.setattr(se_orch, "slack_notify_open_questions", None)
    monkeypatch.setattr(se_orch, "_wait_for_user_answers", lambda jid: False)
    cb = se_orch._build_planning_answer_callback("j")
    assert cb([{"id": "p", "question_text": "Q?"}]) == []


def test_adapter_carries_open_and_resolved_questions():
    result = {
        "success": True,
        "handoff_package": {
            "validated_spec_content": "spec",
            "open_questions": [{"question_text": "Allergen default?"}, "Plain string question?"],
            "resolved_questions": [{"question_id": "q1", "answer": "strict"}],
        },
    }
    out = adapt_planning_result(result, spec_title="P")
    assert out.open_questions == ["Allergen default?", "Plain string question?"]
    assert out.resolved_questions[0]["answer"] == "strict"


def test_adapter_empty_questions_default():
    result = {"success": True, "handoff_package": {"validated_spec_content": "spec"}}
    out = adapt_planning_result(result, spec_title="P")
    assert out.open_questions == []
    assert out.resolved_questions == []


def test_llm_pause_error_prefers_semantic_exhaustion_sentinel():
    """The job-level pause error must surface the semantic-exhaustion remediation
    (simplify/split the prompt) whenever any failed task carries it — the
    connectivity guidance would send the operator into a resume loop."""
    from software_engineering_team.orchestrator import _llm_pause_error
    from software_engineering_team.shared.job_store import (
        LLM_SEMANTIC_EXHAUSTION,
        LLM_UNREACHABLE_AFTER_RETRIES,
    )

    assert _llm_pause_error({"t1": LLM_UNREACHABLE_AFTER_RETRIES}) == LLM_UNREACHABLE_AFTER_RETRIES
    assert _llm_pause_error({"t1": LLM_SEMANTIC_EXHAUSTION}) == LLM_SEMANTIC_EXHAUSTION
    assert (
        _llm_pause_error({"t1": LLM_UNREACHABLE_AFTER_RETRIES, "t2": LLM_SEMANTIC_EXHAUSTION})
        == LLM_SEMANTIC_EXHAUSTION
    )
    # Defensive: unrelated reasons fall back to the connectivity sentinel.
    assert _llm_pause_error({"t1": "build failed"}) == LLM_UNREACHABLE_AFTER_RETRIES
