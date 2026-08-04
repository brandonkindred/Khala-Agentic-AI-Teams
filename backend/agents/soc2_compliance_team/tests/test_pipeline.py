"""Unit tests for the decomposed SOC2 audit pipeline steps.

The pipeline functions are the single source of truth driven by both the
thread-mode orchestrator and the Temporal activities. LLM access is stubbed by
patching ``pipeline.get_client`` so no model is invoked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from soc2_compliance_team import pipeline
from soc2_compliance_team.models import (
    FindingSeverity,
    NextStepsDocument,
    RepoContext,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
    TSCFinding,
)

from .conftest import FakeLLM as _FakeLLM
from .conftest import default_repo_context as _ctx


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response: Dict[str, Any]) -> _FakeLLM:
    llm = _FakeLLM(response)
    monkeypatch.setattr(pipeline, "get_client", lambda _key: llm)
    return llm


# ---------------------------------------------------------------------------
# load_context
# ---------------------------------------------------------------------------


def test_load_context_reads_repo(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')")
    ctx = pipeline.load_context(tmp_path)
    assert isinstance(ctx, RepoContext)
    assert ctx.repo_path == str(tmp_path.resolve())
    assert "main.py" in ctx.code_summary


def test_load_context_invalid_path_raises() -> None:
    with pytest.raises(ValueError, match="not a directory"):
        pipeline.load_context("/nonexistent/path/soc2-xyz")


def test_load_context_empty_directory(tmp_path: Path) -> None:
    """An empty repo yields a valid, empty ``RepoContext`` (no crash)."""
    ctx = pipeline.load_context(tmp_path)
    assert isinstance(ctx, RepoContext)
    assert ctx.file_list == []
    assert ctx.repo_path == str(tmp_path.resolve())


# ---------------------------------------------------------------------------
# audit_criterion / audit_criterion_safe
# ---------------------------------------------------------------------------


def test_audit_criterion_returns_typed_result(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _patch_llm(
        monkeypatch,
        {
            "summary": "Reviewed",
            "findings": [{"severity": "high", "title": "No MFA", "description": "x"}],
            "compliant": False,
        },
    )
    out = pipeline.audit_criterion(TSCCategory.SECURITY, _ctx())
    assert isinstance(out, TSCAuditResult)
    assert out.category is TSCCategory.SECURITY
    assert out.compliant is False
    assert out.findings[0].severity is FindingSeverity.HIGH
    # Locks in the documented "exactly two sequential LLM calls" postcondition
    # (one think=True reasoning call, one think=False formatting call) at the
    # pipeline entry point both drivers actually call.
    assert len(llm.reasoning_calls) == 1
    assert len(llm.calls) == 1


def test_audit_criterion_rejects_unknown_category(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, {})

    class _Bogus:
        value = "bogus"

    with pytest.raises(KeyError):
        pipeline.audit_criterion(_Bogus(), _ctx())  # type: ignore[arg-type]


def test_audit_criterion_safe_isolates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_key: str) -> Any:
        raise RuntimeError("no llm configured")

    monkeypatch.setattr(pipeline, "get_client", _boom)
    out = pipeline.audit_criterion_safe(TSCCategory.PRIVACY, _ctx())
    assert out.category is TSCCategory.PRIVACY
    # Fail-closed: an un-assessable criterion is non-compliant, not a pass.
    assert out.compliant is False
    assert "could not be completed" in out.summary.lower()
    # A synthetic finding makes the failure visible in structured results.
    assert len(out.findings) == 1
    assert out.findings[0].severity is FindingSeverity.HIGH
    assert out.findings[0].category is TSCCategory.PRIVACY


def test_audit_criterion_safe_surfaces_unknown_category() -> None:
    """A bad category is a contract bug — it must raise, not become a placeholder."""

    class _Bogus:
        value = "bogus"

    with pytest.raises(KeyError):
        pipeline.audit_criterion_safe(_Bogus(), _ctx())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_all_criteria
# ---------------------------------------------------------------------------


def test_run_all_criteria_returns_all_five_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, {"summary": "ok", "findings": [], "compliant": True})
    results = pipeline.run_all_criteria(_ctx())
    assert [r.category for r in results] == pipeline.TSC_CRITERIA
    assert len(results) == 5


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------


def _result(category: TSCCategory, *, compliant: bool, severity: FindingSeverity | None = None):
    findings = []
    if severity is not None:
        findings.append(
            TSCFinding(severity=severity, category=category, title="t", description="d")
        )
    return TSCAuditResult(category=category, summary="s", findings=findings, compliant=compliant)


def test_write_report_produces_compliance_report_when_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _patch_llm(
        monkeypatch,
        {
            "executive_summary": "summary",
            "scope": "scope",
            "recommendations_summary": ["fix it"],
            "raw_markdown": "# report",
        },
    )
    tsc = [_result(TSCCategory.SECURITY, compliant=False, severity=FindingSeverity.HIGH)]
    report, next_steps = pipeline.write_report("/repo", tsc)
    assert isinstance(report, SOC2ComplianceReport)
    assert next_steps is None
    # Locks in the documented "exactly two sequential LLM calls" postcondition
    # at the pipeline entry point both drivers actually call.
    assert len(llm.reasoning_calls) == 1
    assert len(llm.calls) == 1


def test_write_report_produces_next_steps_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _patch_llm(
        monkeypatch,
        {
            "title": "Next Steps",
            "introduction": "intro",
            "steps": [{"title": "engage CPA", "description": "d"}],
            "recommended_timeline": "3 months",
            "raw_markdown": "# next",
        },
    )
    tsc = [_result(TSCCategory.SECURITY, compliant=True)]
    report, next_steps = pipeline.write_report("/repo", tsc)
    assert report is None
    assert next_steps is not None
    assert next_steps.title == "Next Steps"
    # Locks in the documented "exactly two sequential LLM calls" postcondition
    # at the pipeline entry point both drivers actually call.
    assert len(llm.reasoning_calls) == 1
    assert len(llm.calls) == 1


def test_write_report_handles_malformed_llm_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/malformed LLM response yields a report with safe defaults, not a crash."""
    _patch_llm(monkeypatch, {})  # LLM returns an empty dict
    tsc = [_result(TSCCategory.SECURITY, compliant=False, severity=FindingSeverity.HIGH)]
    report, next_steps = pipeline.write_report("/repo", tsc)
    assert report is not None
    assert next_steps is None
    # Fields fall back to safe defaults rather than raising.
    assert report.executive_summary == ""
    assert report.recommendations_summary == []


# ---------------------------------------------------------------------------
# assemble_result
# ---------------------------------------------------------------------------


def test_assemble_result_sets_has_findings_true() -> None:
    tsc = [_result(TSCCategory.SECURITY, compliant=False, severity=FindingSeverity.CRITICAL)]
    report = SOC2ComplianceReport(executive_summary="x")
    out = pipeline.assemble_result("/repo", tsc, report, None)
    assert out.status == "completed"
    assert out.has_findings is True
    assert out.compliance_report is report
    assert out.next_steps_document is None


def test_assemble_result_sets_has_findings_false() -> None:
    tsc = [_result(TSCCategory.AVAILABILITY, compliant=True)]
    out = pipeline.assemble_result("/repo", tsc, None, None)
    assert out.has_findings is False
    assert out.status == "completed"


def test_assemble_result_has_findings_follows_report_not_tsc() -> None:
    """has_findings tracks the report writer's decision (which document it
    produced), not a second recomputation over tsc_results."""
    tsc = [_result(TSCCategory.SECURITY, compliant=False, severity=FindingSeverity.CRITICAL)]
    next_steps = NextStepsDocument(title="Next Steps")
    out = pipeline.assemble_result("/repo", tsc, None, next_steps)
    # Report writer chose next-steps ⇒ has_findings is False even though a tsc
    # result is non-compliant — the two can never disagree now.
    assert out.has_findings is False
    assert out.next_steps_document is next_steps


# ---------------------------------------------------------------------------
# failed_result
# ---------------------------------------------------------------------------


def test_failed_result_has_findings_false_with_no_preserved_results() -> None:
    out = pipeline.failed_result("/repo", "boom")
    assert out.status == "failed"
    assert out.tsc_results == []
    assert out.has_findings is False


def test_failed_result_derives_has_findings_from_preserved_tsc_results() -> None:
    """A failure that occurs *after* the criteria audits already found a
    material gap (e.g. report synthesis itself fails) must not report
    itself as clean just because the failure path hardcodes has_findings."""
    tsc = [_result(TSCCategory.SECURITY, compliant=False, severity=FindingSeverity.HIGH)]
    out = pipeline.failed_result("/repo", "report synthesis boom", tsc_results=tsc)
    assert out.status == "failed"
    assert out.has_findings is True
    assert out.tsc_results == tsc


def test_failed_result_has_findings_false_when_preserved_results_are_clean() -> None:
    tsc = [_result(TSCCategory.AVAILABILITY, compliant=True)]
    out = pipeline.failed_result("/repo", "boom", tsc_results=tsc)
    assert out.has_findings is False
