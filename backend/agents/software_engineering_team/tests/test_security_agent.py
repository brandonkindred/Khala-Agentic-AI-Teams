"""Tests for CybersecurityExpertAgent (Strands-migrated).

End-to-end runs against ``DummyLLMClient`` plus direct coverage of the
``approved`` re-derivation policy and the graceful fallback on LLM
failures.
"""

from __future__ import annotations

from security_agent import CybersecurityExpertAgent
from security_agent.models import SecurityInput, SecurityOutput

from llm_service.clients.dummy import DummyLLMClient


def _input(**overrides: object) -> SecurityInput:
    base = {
        "code": "import os\n\ndef run(cmd):\n    os.system(cmd)",
        "language": "python",
        "task_description": "Security review of command runner",
    }
    base.update(overrides)
    return SecurityInput(**base)  # type: ignore[arg-type]


def test_security_agent_default_run_returns_security_output() -> None:
    agent = CybersecurityExpertAgent(DummyLLMClient())
    result = agent.run(_input())
    assert isinstance(result, SecurityOutput)
    # Dummy stub returns no vulnerabilities, so approved is True.
    assert result.vulnerabilities == []
    assert result.approved is True
    assert "dummy" in result.summary.lower()


def test_security_agent_with_context_and_architecture() -> None:
    """Optional context and architecture fields should not crash the pipeline."""
    from software_engineering_team.shared.models import SystemArchitecture

    arch = SystemArchitecture(
        overview="Tiny microservice",
        architecture_document="# Arch\n\nSingle FastAPI service.",
        components=[],
        decisions=[],
        diagrams={},
    )
    agent = CybersecurityExpertAgent(DummyLLMClient())
    result = agent.run(
        _input(
            context="Runs behind reverse proxy",
            architecture=arch,
        )
    )
    assert isinstance(result, SecurityOutput)
    assert result.approved is True


def test_security_agent_derives_approved_from_severities() -> None:
    """If the LLM returns critical vulnerabilities with approved=True, the
    agent must override approved to False."""

    class _LyingClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "vulnerabilities": [
                    {
                        "severity": "critical",
                        "category": "injection",
                        "description": "Command injection in run()",
                        "location": "run:3",
                        "recommendation": "Use subprocess with shell=False",
                    },
                    {
                        "severity": "low",
                        "category": "style",
                        "description": "nitpick",
                        "recommendation": "rename var",
                    },
                ],
                "approved": True,  # deliberately wrong — agent should override
                "summary": "LGTM",
                "remediations": [],
                "suggested_commit_message": "",
            }

    agent = CybersecurityExpertAgent(_LyingClient())
    result = agent.run(_input())
    assert result.approved is False
    assert len(result.vulnerabilities) == 2
    assert result.vulnerabilities[0].severity == "critical"
    assert result.vulnerabilities[0].category == "injection"


def test_multiple_run_calls_on_same_instance_succeed() -> None:
    """Regression: a single ``CybersecurityExpertAgent`` instance must
    handle many sequential ``run()`` calls. See
    test_code_review_agent.py::test_multiple_run_calls_on_same_instance_succeed
    for the root-cause details."""
    agent = CybersecurityExpertAgent(DummyLLMClient())
    for i in range(4):
        result = agent.run(_input(code=f"def f{i}(): return {i}"))
        assert isinstance(result, SecurityOutput), f"run {i} did not return SecurityOutput"
        assert result.approved is True, f"run {i} failed: {result.summary}"


def test_security_agent_only_critical_high_flip_approved() -> None:
    """Medium/low-only vulnerabilities should keep approved=True."""

    class _MediumOnlyClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "vulnerabilities": [
                    {
                        "severity": "medium",
                        "category": "config",
                        "description": "verbose logs",
                        "recommendation": "mask secrets",
                    },
                    {
                        "severity": "low",
                        "category": "style",
                        "description": "nit",
                        "recommendation": "...",
                    },
                ],
                "summary": "Minor findings only",
                "remediations": [],
                "suggested_commit_message": "",
            }

    agent = CybersecurityExpertAgent(_MediumOnlyClient())
    result = agent.run(_input())
    assert result.approved is True
    assert len(result.vulnerabilities) == 2
