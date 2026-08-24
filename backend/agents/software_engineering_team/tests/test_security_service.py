"""Tests for the unified Security Review service.

Covers the centralized severity/blocking rule, the profile-parameterized
prompt assembly (incl. an equivalence guard locking the migrated prompts to the
text the legacy tool/devsecops tests rely on), the infra gate combiner, and the
policy-as-code hook (via an injected stub — no real checkov).
"""

from __future__ import annotations

import pytest

from software_engineering_team.shared.security_service import (
    BLOCKING_SEVERITIES,
    CODE_BACKEND_FOCUS,
    CODE_FRONTEND_FOCUS,
    SecurityProfile,
    any_blocking,
    build_review_prompt,
    derive_approved,
    infra_gate_passed,
    is_blocking,
    run_policy_scan,
)


class _Finding:
    """Minimal stand-in exposing ``severity`` and (optionally) ``blocking``."""

    def __init__(self, severity: str, blocking: bool = False) -> None:
        self.severity = severity
        self.blocking = blocking


class _Vuln:
    """Stand-in with no ``blocking`` attribute (like ``SecurityVulnerability``)."""

    def __init__(self, severity: str) -> None:
        self.severity = severity


# --- is_blocking ----------------------------------------------------------
@pytest.mark.parametrize("sev", sorted(BLOCKING_SEVERITIES))
def test_is_blocking_true_for_blocking_severities(sev: str) -> None:
    assert is_blocking(sev) is True


@pytest.mark.parametrize("sev", ["medium", "low", "info", "minor", "nit", ""])
def test_is_blocking_false_for_non_blocking_severities(sev: str) -> None:
    assert is_blocking(sev) is False


def test_is_blocking_honors_explicit_flag_on_low_severity() -> None:
    assert is_blocking("low", explicit_blocking=True) is True


def test_is_blocking_is_case_insensitive() -> None:
    assert is_blocking("High") is True


# --- any_blocking ---------------------------------------------------------
def test_any_blocking_empty_is_false() -> None:
    assert any_blocking([]) is False


def test_any_blocking_detects_high_severity() -> None:
    assert any_blocking([_Finding("low"), _Finding("high")]) is True


def test_any_blocking_detects_explicit_flag_without_blocking_attr_default() -> None:
    # A low-severity finding flagged blocking still blocks.
    assert any_blocking([_Finding("low", blocking=True)]) is True


def test_any_blocking_works_without_blocking_attribute() -> None:
    assert any_blocking([_Vuln("medium"), _Vuln("low")]) is False
    assert any_blocking([_Vuln("critical")]) is True


# --- derive_approved ------------------------------------------------------
def test_derive_approved_blocks_on_severity_regardless_of_llm_flag() -> None:
    # Mirrors the "LLM lied" case: a high finding overrides approved=True.
    assert derive_approved([_Finding("high")], llm_approved=True) is False


def test_derive_approved_blocks_on_explicit_flag() -> None:
    assert derive_approved([_Finding("low", blocking=True)], llm_approved=True) is False


def test_derive_approved_medium_low_only_stays_approved() -> None:
    assert derive_approved([_Vuln("medium"), _Vuln("low")], llm_approved=None) is True


def test_derive_approved_empty_defaults_true() -> None:
    assert derive_approved([], llm_approved=None) is True


def test_derive_approved_honors_llm_false_when_not_blocking() -> None:
    assert derive_approved([_Finding("low")], llm_approved=False) is False


def test_derive_approved_honors_llm_true_when_not_blocking() -> None:
    assert derive_approved([_Finding("low")], llm_approved=True) is True


# --- build_review_prompt --------------------------------------------------
def test_build_code_prompt_backend_contains_focus_and_slots() -> None:
    prompt = build_review_prompt(SecurityProfile.CODE, focus=CODE_BACKEND_FOCUS)
    assert "1. Injection — SQL, command, or template injection" in prompt
    assert "Authentication/authorisation" in prompt
    # The lifecycle formats these later, so the literal slots must survive.
    assert "{task_description}" in prompt
    assert "{code}" in prompt
    assert "## ISSUES ##" in prompt
    assert "severity: critical|high|medium|low|info" in prompt


def test_build_code_prompt_frontend_uses_frontend_focus() -> None:
    prompt = build_review_prompt(SecurityProfile.CODE, focus=CODE_FRONTEND_FOCUS)
    assert "XSS — unescaped user input" in prompt
    assert "Content Security Policy (CSP)" in prompt
    assert "Injection — SQL" not in prompt


def test_build_code_prompt_accepts_string_profile() -> None:
    assert build_review_prompt("code", focus=CODE_BACKEND_FOCUS).startswith(
        "You are an expert Security specialist."
    )


def test_build_code_prompt_requires_focus() -> None:
    with pytest.raises(ValueError, match="requires a non-empty 'focus'"):
        build_review_prompt(SecurityProfile.CODE)


def test_build_infra_prompt_contains_focus_and_json_contract() -> None:
    prompt = build_review_prompt(SecurityProfile.INFRA)
    assert prompt.startswith("You are DevSecOpsReviewAgent.")
    assert "IAM least privilege and trust policy safety" in prompt
    assert "artifact integrity controls (scan/SBOM/signing references)" in prompt
    assert "Return JSON only." in prompt
    assert "blocking, exploitability" in prompt


def test_build_infra_prompt_rejects_focus_argument() -> None:
    with pytest.raises(ValueError, match="does not accept a 'focus'"):
        build_review_prompt(SecurityProfile.INFRA, focus=CODE_BACKEND_FOCUS)


def test_build_review_prompt_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError):
        build_review_prompt("bogus", focus="x")


def test_migrated_constants_match_service_output() -> None:
    """Equivalence guard: the team prompt constants are exactly what the
    service builds, so the legacy text the existing tests parse is unchanged."""
    from software_engineering_team.codegen_team.stacks.backend.prompts import (
        SECURITY_TOOL_AGENT_REVIEW_PROMPT as backend_prompt,
    )
    from software_engineering_team.devops_team.devsecops_review_agent.prompts import (
        DEVSECOPS_REVIEW_PROMPT,
    )
    from software_engineering_team.codegen_team.stacks.frontend.prompts import (
        SECURITY_TOOL_AGENT_REVIEW_PROMPT as frontend_prompt,
    )

    assert backend_prompt == build_review_prompt(SecurityProfile.CODE, focus=CODE_BACKEND_FOCUS)
    assert frontend_prompt == build_review_prompt(SecurityProfile.CODE, focus=CODE_FRONTEND_FOCUS)
    assert DEVSECOPS_REVIEW_PROMPT == build_review_prompt(SecurityProfile.INFRA)


# --- infra_gate_passed ----------------------------------------------------
@pytest.mark.parametrize(
    "devsec,policy,expected",
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_infra_gate_passed_truth_table(devsec: bool, policy: bool, expected: bool) -> None:
    assert infra_gate_passed(devsec, policy) is expected


# --- run_policy_scan ------------------------------------------------------
class _StubPolicyRunner:
    def __init__(self, output: object) -> None:
        self._output = output
        self.calls: list[str] = []

    def run(self, input_data: object) -> object:
        self.calls.append(input_data.repo_path)  # type: ignore[attr-defined]
        return self._output


def test_run_policy_scan_uses_injected_runner() -> None:
    from software_engineering_team.devops_team.tool_agents.policy_as_code import PolicyAsCodeOutput

    canned = PolicyAsCodeOutput(success=True, checks={"policy_checks": "skipped"})
    runner = _StubPolicyRunner(canned)
    result = run_policy_scan("/repo", runner=runner)
    assert result is canned
    assert runner.calls == ["/repo"]


def test_run_policy_scan_requires_repo_path() -> None:
    with pytest.raises(AssertionError):
        run_policy_scan("", runner=_StubPolicyRunner(None))


def test_run_policy_scan_default_runner_constructed_lazily(monkeypatch) -> None:
    import software_engineering_team.devops_team.tool_agents.policy_as_code as pac

    sentinel = pac.PolicyAsCodeOutput(success=False, checks={"policy_checks": "fail"})

    class _FakeAgent:
        def run(self, input_data: object) -> object:
            return sentinel

    monkeypatch.setattr(pac, "PolicyAsCodeToolAgent", _FakeAgent)
    assert run_policy_scan("/repo") is sentinel
