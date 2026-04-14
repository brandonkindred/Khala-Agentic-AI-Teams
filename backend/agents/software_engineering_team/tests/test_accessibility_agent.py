"""Tests for AccessibilityExpertAgent (Strands-migrated)."""

from __future__ import annotations

from accessibility_agent import AccessibilityExpertAgent
from accessibility_agent.models import AccessibilityInput, AccessibilityOutput

from llm_service.clients.dummy import DummyLLMClient


def _input(**overrides: object) -> AccessibilityInput:
    base = {
        "code": '<button onclick="x()">Click</button>',
        "language": "html",
        "task_description": "Accessibility review of click handler",
    }
    base.update(overrides)
    return AccessibilityInput(**base)  # type: ignore[arg-type]


def test_accessibility_agent_default_run_returns_accessibility_output() -> None:
    agent = AccessibilityExpertAgent(DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, AccessibilityOutput)
    # Dummy stub returns no issues.
    assert result.issues == []
    assert result.approved is True
    assert "wcag" in result.summary.lower() or "dummy" in result.summary.lower()


def test_accessibility_agent_derives_approved_from_severities() -> None:
    """Critical/high issues flip approved to False even when LLM says True."""

    class _LyingClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "issues": [
                    {
                        "severity": "critical",
                        "wcag_criterion": "1.1.1",
                        "description": "Missing alt text on img",
                        "location": "home.tsx:42",
                        "recommendation": "Add meaningful alt= attribute",
                    },
                    {
                        "severity": "low",
                        "wcag_criterion": "1.4.3",
                        "description": "Low contrast nit",
                        "recommendation": "Bump contrast ratio",
                    },
                ],
                "approved": True,  # deliberately wrong
                "summary": "LGTM",
            }

    agent = AccessibilityExpertAgent(_LyingClient())
    result = agent.run(_input())
    assert result.approved is False
    assert len(result.issues) == 2
    assert result.issues[0].wcag_criterion == "1.1.1"


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``AccessibilityExpertAgent`` instance must
    handle many sequential ``run()`` calls. See
    test_code_review_agent.py::test_multiple_run_calls_on_same_instance_succeed
    for the root-cause details."""
    agent = AccessibilityExpertAgent(DummyLLMClient())
    for i in range(4):
        result = agent.run(_input(code=f"<button>Click {i}</button>"))
        assert isinstance(result, AccessibilityOutput), (
            f"run {i} did not return AccessibilityOutput"
        )
        assert result.approved is True, f"run {i} failed: {result.summary}"


def test_accessibility_agent_medium_only_keeps_approved_true() -> None:
    class _MediumOnlyClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "issues": [
                    {
                        "severity": "medium",
                        "wcag_criterion": "2.4.4",
                        "description": "Ambiguous link text",
                        "recommendation": "Rename link",
                    },
                ],
                "summary": "One medium finding",
            }

    agent = AccessibilityExpertAgent(_MediumOnlyClient())
    result = agent.run(_input())
    assert result.approved is True
    assert len(result.issues) == 1
