"""Tests for the SOC2 specialist agent classes and report writer.

These cover the legacy class-based agents (which the orchestrator's Graph
path bypasses but which are still part of the public surface) plus the
Strands ``make_*`` factories. All LLM calls are stubbed with a fake
client so no model is invoked.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from soc2_compliance_team.agents import (
    AvailabilityTSCAgent,
    ConfidentialityTSCAgent,
    PrivacyTSCAgent,
    ProcessingIntegrityTSCAgent,
    ReportWriterAgent,
    SecurityTSCAgent,
    _parse_finding,
    _run_tsc_agent,
    make_availability_tsc_agent,
    make_confidentiality_tsc_agent,
    make_privacy_tsc_agent,
    make_processing_integrity_tsc_agent,
    make_report_writer_agent,
    make_security_tsc_agent,
)
from soc2_compliance_team.models import (
    FindingSeverity,
    RepoContext,
    TSCAuditResult,
    TSCCategory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Minimal LLM stand-in capturing prompts and returning canned JSON."""

    def __init__(self, response: Dict[str, Any], ctx_tokens: int = 16384) -> None:
        self._response = response
        self._ctx_tokens = ctx_tokens
        self.calls: list[Dict[str, Any]] = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float | None = None,
        think: bool | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, "temperature": temperature, "think": think, **kwargs})
        return self._response

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return prompt[: kwargs.get("max_chars", 100)] if "max_chars" in kwargs else prompt

    def get_max_context_tokens(self) -> int:
        return self._ctx_tokens


def _ctx(**overrides: Any) -> RepoContext:
    base = RepoContext(
        repo_path="/repo",
        code_summary="print('hi')",
        readme_content="# Title",
        file_list=["main.py", "README.md"],
        tech_stack_hint="Python",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ---------------------------------------------------------------------------
# _parse_finding
# ---------------------------------------------------------------------------


def test_parse_finding_uses_supplied_severity() -> None:
    f = _parse_finding(
        {
            "severity": "critical",
            "title": "X",
            "description": "Y",
            "location": "L",
            "recommendation": "R",
            "evidence_observed": "E",
        },
        TSCCategory.SECURITY,
    )
    assert f.severity is FindingSeverity.CRITICAL
    assert f.category is TSCCategory.SECURITY
    assert f.title == "X"
    assert f.description == "Y"
    assert f.location == "L"
    assert f.recommendation == "R"
    assert f.evidence_observed == "E"


def test_parse_finding_unknown_severity_falls_back_to_medium() -> None:
    f = _parse_finding({"severity": "purple", "title": "T"}, TSCCategory.PRIVACY)
    assert f.severity is FindingSeverity.MEDIUM
    assert f.category is TSCCategory.PRIVACY


def test_parse_finding_missing_severity_defaults_to_medium() -> None:
    f = _parse_finding({"title": "Untitled"}, TSCCategory.AVAILABILITY)
    assert f.severity is FindingSeverity.MEDIUM
    # Missing fields fall back to defaults / empty strings
    assert f.title == "Untitled"
    assert f.description == ""
    assert f.location == ""
    assert f.recommendation == ""
    assert f.evidence_observed == ""


def test_parse_finding_missing_title_defaults() -> None:
    f = _parse_finding({"description": "stuff"}, TSCCategory.CONFIDENTIALITY)
    assert f.title == "Untitled"
    assert f.description == "stuff"


# ---------------------------------------------------------------------------
# _run_tsc_agent
# ---------------------------------------------------------------------------


def test_run_tsc_agent_parses_response_into_audit_result() -> None:
    llm = _FakeLLM(
        {
            "summary": "S",
            "findings": [
                {
                    "severity": "high",
                    "title": "Missing MFA",
                    "description": "No multi-factor auth",
                    "location": "auth.py",
                    "recommendation": "Add TOTP",
                    "evidence_observed": "single-factor login route",
                },
                {
                    "severity": "low",
                    "title": "TLS version",
                    "description": "TLS 1.2 instead of 1.3",
                },
            ],
            "compliant": False,
        }
    )
    out = _run_tsc_agent(
        llm,
        TSCCategory.SECURITY,
        "Security",
        "auth, encryption",
        _ctx(),
    )
    assert isinstance(out, TSCAuditResult)
    assert out.category is TSCCategory.SECURITY
    assert out.summary == "S"
    assert len(out.findings) == 2
    assert out.findings[0].severity is FindingSeverity.HIGH
    assert out.findings[1].severity is FindingSeverity.LOW
    assert out.compliant is False
    # Prompt should include criterion-specific text
    call = llm.calls[0]
    assert "Security" in call["prompt"]
    assert "auth, encryption" in call["prompt"]
    assert call["temperature"] == 0.1
    assert call["think"] is True


def test_run_tsc_agent_skips_empty_findings_and_invents_compliance() -> None:
    llm = _FakeLLM(
        {
            "summary": "ok",
            "findings": [
                # title/description both blank/missing → skipped
                {"severity": "low"},
                # Not a dict → skipped
                "not a dict",
                # Real finding
                {"severity": "medium", "title": "X"},
            ],
            # compliant omitted → derived from severity set
        }
    )
    out = _run_tsc_agent(
        llm,
        TSCCategory.AVAILABILITY,
        "Availability",
        "uptime",
        _ctx(),
    )
    assert len(out.findings) == 1
    # No critical/high findings → derived compliant=True
    assert out.compliant is True


def test_run_tsc_agent_defaults_when_llm_returns_empty() -> None:
    llm = _FakeLLM({})
    out = _run_tsc_agent(
        llm,
        TSCCategory.PRIVACY,
        "Privacy",
        "PII",
        _ctx(),
    )
    assert out.summary == ""
    assert out.findings == []
    assert out.compliant is True


def test_run_tsc_agent_without_get_max_context_tokens() -> None:
    """If the LLM lacks ``get_max_context_tokens`` the helper falls back to
    a default 16k context budget."""

    class _LLMNoCtx:
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"summary": "ok", "findings": []}

        def complete(self, prompt: str, **kwargs: Any) -> str:
            return prompt

    out = _run_tsc_agent(
        _LLMNoCtx(),
        TSCCategory.CONFIDENTIALITY,
        "Confidentiality",
        "secrets",
        _ctx(),
    )
    assert isinstance(out, TSCAuditResult)
    assert out.summary == "ok"


def test_run_tsc_agent_critical_finding_marks_non_compliant_via_default() -> None:
    """When `compliant` is omitted but there is a critical finding, the
    derived value should be False."""
    llm = _FakeLLM(
        {
            "summary": "s",
            "findings": [
                {"severity": "critical", "title": "Hardcoded secret"},
            ],
        }
    )
    out = _run_tsc_agent(
        llm,
        TSCCategory.SECURITY,
        "Security",
        "secrets",
        _ctx(),
    )
    assert out.compliant is False


# ---------------------------------------------------------------------------
# Per-TSC agent classes — they all just delegate to _run_tsc_agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent_cls", "expected_category"),
    [
        (SecurityTSCAgent, TSCCategory.SECURITY),
        (AvailabilityTSCAgent, TSCCategory.AVAILABILITY),
        (ProcessingIntegrityTSCAgent, TSCCategory.PROCESSING_INTEGRITY),
        (ConfidentialityTSCAgent, TSCCategory.CONFIDENTIALITY),
        (PrivacyTSCAgent, TSCCategory.PRIVACY),
    ],
)
def test_tsc_agent_classes_run(agent_cls, expected_category) -> None:
    llm = _FakeLLM(
        {
            "summary": "ok",
            "findings": [],
            "compliant": True,
        }
    )
    result = agent_cls().run(llm, _ctx())
    assert isinstance(result, TSCAuditResult)
    assert result.category is expected_category
    assert result.compliant is True


# ---------------------------------------------------------------------------
# ReportWriterAgent
# ---------------------------------------------------------------------------


def _audit_result(
    category: TSCCategory,
    *,
    findings_severities: list[FindingSeverity] | None = None,
    compliant: bool = True,
) -> TSCAuditResult:
    findings = []
    for sev in findings_severities or []:
        findings.append(
            {
                "severity": sev,
                "category": category,
                "title": "F",
                "description": "D",
                "location": "L",
                "recommendation": "R",
                "evidence_observed": "E",
            }
        )
    return TSCAuditResult(
        category=category,
        summary="s",
        findings=findings,  # pydantic converts dicts to TSCFinding
        compliant=compliant,
    )


def test_report_writer_returns_next_steps_when_no_findings() -> None:
    llm = _FakeLLM(
        {
            "title": "Next Steps",
            "introduction": "intro",
            "steps": [
                {"title": "Engage CPA", "description": "..."},
                "string-step-becomes-stub",
            ],
            "recommended_timeline": "6 months",
            "raw_markdown": "# Next Steps",
        }
    )
    tsc_results = [
        _audit_result(c, compliant=True)
        for c in (
            TSCCategory.SECURITY,
            TSCCategory.AVAILABILITY,
            TSCCategory.PROCESSING_INTEGRITY,
            TSCCategory.CONFIDENTIALITY,
            TSCCategory.PRIVACY,
        )
    ]
    report, next_steps = ReportWriterAgent().run(llm, "/repo", tsc_results)
    assert report is None
    assert next_steps is not None
    assert next_steps.title == "Next Steps"
    # String steps get coerced into a dict stub
    assert any(s.get("description") == "" for s in next_steps.steps)
    assert next_steps.recommended_timeline == "6 months"


def test_report_writer_next_steps_handles_non_list_steps() -> None:
    llm = _FakeLLM(
        {
            "title": "Next Steps",
            "introduction": "i",
            "steps": "not-a-list",
            "recommended_timeline": "",
            "raw_markdown": "",
        }
    )
    tsc_results = [_audit_result(TSCCategory.SECURITY, compliant=True)]
    report, next_steps = ReportWriterAgent().run(llm, "/repo", tsc_results)
    assert report is None
    assert next_steps is not None
    assert next_steps.steps == []


def test_report_writer_next_steps_defaults_on_empty_llm_response() -> None:
    llm = _FakeLLM({})
    tsc_results = [_audit_result(TSCCategory.SECURITY, compliant=True)]
    report, next_steps = ReportWriterAgent().run(llm, "/repo", tsc_results)
    assert report is None
    assert next_steps is not None
    assert next_steps.title == "Next Steps for SOC2 Certification"


def test_report_writer_returns_compliance_report_when_findings_exist() -> None:
    llm = _FakeLLM(
        {
            "executive_summary": "exec summary",
            "scope": "scope para",
            "recommendations_summary": ["fix X", "fix Y"],
            "raw_markdown": "# Report",
        }
    )
    tsc_results = [
        _audit_result(
            TSCCategory.SECURITY,
            findings_severities=[FindingSeverity.HIGH],
            compliant=False,
        ),
        _audit_result(TSCCategory.AVAILABILITY, compliant=True),
    ]
    report, next_steps = ReportWriterAgent().run(llm, "/repo", tsc_results)
    assert next_steps is None
    assert report is not None
    assert report.executive_summary == "exec summary"
    assert report.scope == "scope para"
    assert report.recommendations_summary == ["fix X", "fix Y"]
    assert TSCCategory.SECURITY.value in report.findings_by_tsc
    assert len(report.findings_by_tsc[TSCCategory.SECURITY.value]) == 1


def test_report_writer_defaults_scope_on_empty_llm_response() -> None:
    llm = _FakeLLM({})
    tsc_results = [
        _audit_result(
            TSCCategory.SECURITY,
            findings_severities=[FindingSeverity.CRITICAL],
            compliant=False,
        )
    ]
    report, _ = ReportWriterAgent().run(llm, "/some/repo", tsc_results)
    assert report is not None
    # When LLM gave no scope, the writer falls back to a default referencing the repo
    assert "/some/repo" in report.scope


def test_report_writer_handles_invalid_finding_dicts() -> None:
    """If the per-category findings list contains invalid dicts, the
    writer's typed conversion falls back to an empty list rather than
    raising."""
    # Manually construct an audit result whose internal findings are
    # legitimate TSCFinding instances; we'll then patch the
    # ``findings_by_tsc`` building step indirectly by passing an LLM
    # response containing nothing — the *inputs* dict comes from
    # ``r.findings`` via .model_dump(), so to exercise the except branch
    # we monkey-patch the conversion path via the agent's private helper.
    agent = ReportWriterAgent()
    llm = _FakeLLM(
        {
            "executive_summary": "es",
            "scope": "sc",
            "recommendations_summary": [],
            "raw_markdown": "rm",
        }
    )
    # Pass a malformed findings_by_tsc directly to the inner helper to
    # cover the except branch.
    bad_findings = {"security": [{"missing_required_fields": True}]}
    tsc_results = [
        _audit_result(
            TSCCategory.SECURITY,
            findings_severities=[FindingSeverity.HIGH],
            compliant=False,
        )
    ]
    report = agent._produce_compliance_report(llm, "/r", tsc_results, bad_findings)
    # Conversion failure ⇒ typed list for security becomes []
    assert report.findings_by_tsc["security"] == []


# ---------------------------------------------------------------------------
# Strands Agent factories — these just build agents; cover the call paths
# ---------------------------------------------------------------------------


def test_make_tsc_agent_factories_return_agent_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``build_agent`` so we can verify the names/prompts without
    constructing a real Strands Agent."""
    import soc2_compliance_team.agents as amod

    captured: list[Dict[str, Any]] = []

    class _FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def _fake_build_agent(*, name, system_prompt, agent_key, description):
        captured.append(
            {
                "name": name,
                "system_prompt": system_prompt,
                "agent_key": agent_key,
                "description": description,
            }
        )
        return _FakeAgent(
            name=name,
            system_prompt=system_prompt,
            agent_key=agent_key,
            description=description,
        )

    monkeypatch.setattr(amod, "build_agent", _fake_build_agent)

    a1 = make_security_tsc_agent()
    a2 = make_availability_tsc_agent()
    a3 = make_processing_integrity_tsc_agent()
    a4 = make_confidentiality_tsc_agent()
    a5 = make_privacy_tsc_agent()
    a6 = make_report_writer_agent()

    assert all(isinstance(a, _FakeAgent) for a in (a1, a2, a3, a4, a5, a6))
    names = [c["name"] for c in captured]
    assert "security_(common_criteria)_tsc_agent" in names
    assert "availability_tsc_agent" in names
    assert "processing_integrity_tsc_agent" in names
    assert "confidentiality_tsc_agent" in names
    assert "privacy_tsc_agent" in names
    assert "soc2_report_writer" in names
    # All agents are tagged with the same agent_key
    assert all(c["agent_key"] == "soc2" for c in captured)
    # System prompts reference the criterion name (where appropriate)
    sec_prompt = next(c for c in captured if "security" in c["name"])["system_prompt"]
    assert "Security (Common Criteria)" in sec_prompt
