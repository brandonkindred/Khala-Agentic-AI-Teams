"""Tests for AcceptanceVerifierAgent (Strands-migrated)."""

from __future__ import annotations

from acceptance_verifier_agent import AcceptanceVerifierAgent
from acceptance_verifier_agent.models import (
    AcceptanceVerifierInput,
    AcceptanceVerifierOutput,
)

from llm_service.clients.dummy import DummyLLMClient


def _input(**overrides: object) -> AcceptanceVerifierInput:
    base = {
        "code": "def add(a, b):\n    return a + b",
        "task_description": "Implement add(a, b)",
        "acceptance_criteria": [
            "add(1, 2) returns 3",
            "add(0, 0) returns 0",
        ],
        "language": "python",
    }
    base.update(overrides)
    return AcceptanceVerifierInput(**base)  # type: ignore[arg-type]


def test_acceptance_verifier_default_run_returns_output() -> None:
    agent = AcceptanceVerifierAgent(DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, AcceptanceVerifierOutput)
    # Dummy stub reports every criterion satisfied.
    assert result.all_satisfied is True
    assert len(result.per_criterion) >= 1


def test_acceptance_verifier_short_circuits_on_empty_criteria() -> None:
    """No criteria → no LLM call, always all_satisfied with empty list."""

    class _TripWireClient(DummyLLMClient):
        def complete_json(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("LLM must not be called when criteria is empty")

        def chat_json_round(self, *a, **kw):  # type: ignore[override]
            raise AssertionError("LLM must not be called when criteria is empty")

    agent = AcceptanceVerifierAgent(_TripWireClient())
    result = agent.run(_input(acceptance_criteria=[]))
    assert isinstance(result, AcceptanceVerifierOutput)
    assert result.all_satisfied is True
    assert result.per_criterion == []
    assert "no criteria" in result.summary.lower()


def test_acceptance_verifier_derives_all_satisfied_from_per_criterion() -> None:
    """If any criterion is unsatisfied, all_satisfied must be False even
    when the LLM sets the top-level flag to True."""

    class _LyingClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "per_criterion": [
                    {
                        "criterion": "add(1, 2) returns 3",
                        "satisfied": True,
                        "evidence": "Code returns a+b",
                    },
                    {
                        "criterion": "add(0, 0) returns 0",
                        "satisfied": False,
                        "evidence": "No test coverage",
                    },
                ],
                "all_satisfied": True,  # deliberately wrong
                "summary": "Looks good",
            }

    agent = AcceptanceVerifierAgent(_LyingClient())
    result = agent.run(_input())
    assert result.all_satisfied is False
    assert len(result.per_criterion) == 2
    assert result.per_criterion[1].satisfied is False


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``AcceptanceVerifierAgent`` instance must
    handle many sequential ``run()`` calls. See
    test_code_review_agent.py::test_multiple_run_calls_on_same_instance_succeed
    for the root-cause details."""
    agent = AcceptanceVerifierAgent(DummyLLMClient())
    for i in range(4):
        result = agent.run(_input(task_description=f"Task {i}"))
        assert isinstance(result, AcceptanceVerifierOutput), (
            f"run {i} did not return AcceptanceVerifierOutput"
        )
        assert result.all_satisfied is True, f"run {i} failed: {result.summary}"


def test_acceptance_verifier_respects_all_satisfied_when_per_criterion_empty() -> None:
    """If the LLM returns an empty per_criterion list but sets all_satisfied,
    we trust the top-level flag (no re-derivation possible)."""

    class _NoDetailClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "per_criterion": [],
                "all_satisfied": False,
                "summary": "Could not evaluate",
            }

    agent = AcceptanceVerifierAgent(_NoDetailClient())
    result = agent.run(_input())
    assert result.all_satisfied is False
    assert result.per_criterion == []
