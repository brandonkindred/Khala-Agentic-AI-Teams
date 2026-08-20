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
    from shared.dev_models.models import SystemArchitecture

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
            }

    agent = CybersecurityExpertAgent(_MediumOnlyClient())
    result = agent.run(_input())
    assert result.approved is True
    assert len(result.vulnerabilities) == 2


def test_security_agent_recovers_from_malformed_first_response() -> None:
    """A schema-invalid first reply drives ``complete_validated``'s corrective
    retry; a valid second reply is used instead of falling back."""

    class _RecoveringClient(DummyLLMClient):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            self.calls += 1
            if self.calls == 1:
                # Missing required top-level fields (summary, remediations)
                # -- schema-invalid.
                return {"vulnerabilities": []}
            return {
                "vulnerabilities": [],
                "summary": "Recovered on retry",
                "remediations": [],
            }

    client = _RecoveringClient()
    agent = CybersecurityExpertAgent(client)
    result = agent.run(_input())

    assert isinstance(result, SecurityOutput)
    assert result.approved is True
    assert result.summary == "Recovered on retry"
    assert client.calls == 2


def test_security_agent_falls_back_when_retries_exhausted() -> None:
    """A reply that stays schema-invalid across the corrective retry still
    yields the safe fallback -- never raises out of ``run()``."""

    class _AlwaysBrokenClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {"vulnerabilities": []}  # missing required fields every call

    agent = CybersecurityExpertAgent(_AlwaysBrokenClient())
    result = agent.run(_input())

    assert isinstance(result, SecurityOutput)
    assert result.approved is False
    assert result.vulnerabilities == []
    assert "Security analysis failed" in result.summary


def test_security_agent_blocks_on_capitalized_severity() -> None:
    """Severity gating is case-insensitive: a capitalized ``"High"`` (the field
    is free-form text, not a validated lowercase Literal) still blocks."""

    class _CapitalizedClient(DummyLLMClient):
        def complete_json(
            self, prompt, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
        ):  # type: ignore[override]
            return {
                "vulnerabilities": [
                    {
                        "severity": "High",
                        "category": "auth",
                        "description": "missing authz check",
                        "recommendation": "enforce authorization",
                    },
                ],
                "summary": "One high finding (capitalized)",
                "remediations": [],
            }

    agent = CybersecurityExpertAgent(_CapitalizedClient())
    result = agent.run(_input())
    assert result.approved is False


# ---------------------------------------------------------------------------
# _build_user_prompt: shared file context as a stable prefix
# ---------------------------------------------------------------------------


def test_file_context_prefix_precedes_role_instructions() -> None:
    """The shared microtask file context (language + code) is a stable prefix
    ahead of the role-specific instructions (schema hint, task, context,
    architecture) -- pure reorder/isolation, no cache marking yet."""
    from shared.dev_models.models import SystemArchitecture

    prompt = CybersecurityExpertAgent._build_user_prompt(
        _input(
            context="Runs behind reverse proxy",
            architecture=SystemArchitecture(overview="layered"),
        )
    )
    code_pos = prompt.index("os.system(cmd)")
    assert code_pos < prompt.index("Review the code for security vulnerabilities")
    assert code_pos < prompt.index("**Task:**")
    assert code_pos < prompt.index("**Context:**")
    assert code_pos < prompt.index("**Architecture:**")
    # DummyLLMClient's pattern-anchor regression guard: both words must still
    # appear somewhere in the prompt (order-independent substring match).
    assert "security" in prompt.lower()
    assert "vulnerabilities" in prompt.lower()
