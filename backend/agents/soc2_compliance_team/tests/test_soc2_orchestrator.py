"""Tests for the SOC2 audit orchestrator (thread-mode driver).

The orchestrator drives the shared pipeline steps; here the pipeline functions
are patched so no LLM is invoked, and the orchestration paths (success with and
without findings, repo-load failure, pipeline failure) are exercised directly.
"""

from pathlib import Path

import pytest

from soc2_compliance_team import pipeline
from soc2_compliance_team.models import (
    FindingSeverity,
    NextStepsDocument,
    RepoContext,
    SOC2AuditResult,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
    TSCFinding,
)
from soc2_compliance_team.orchestrator import SOC2AuditOrchestrator, run_soc2_audit
from soc2_compliance_team.repo_loader import load_repo_context


def test_load_repo_context(tmp_path: Path) -> None:
    """Repo loader returns RepoContext with code_summary and file_list."""
    (tmp_path / "README.md").write_text("# Test repo")
    (tmp_path / "main.py").write_text("print('hello')")
    ctx = load_repo_context(tmp_path)
    assert ctx.repo_path == str(tmp_path.resolve())
    assert "main.py" in ctx.code_summary
    assert "README.md" in ctx.file_list or "readme_content" in ctx.readme_content


def test_load_repo_context_invalid_path() -> None:
    """Repo loader raises for non-directory."""
    with pytest.raises(ValueError, match="not a directory"):
        load_repo_context("/nonexistent/path/12345")


def _clean_results() -> list[TSCAuditResult]:
    return [
        TSCAuditResult(category=c, summary="ok", findings=[], compliant=True)
        for c in pipeline.TSC_CRITERIA
    ]


def _results_with_findings() -> list[TSCAuditResult]:
    results = _clean_results()
    results[0] = TSCAuditResult(
        category=TSCCategory.SECURITY,
        summary="gap",
        findings=[
            TSCFinding(
                severity=FindingSeverity.HIGH,
                category=TSCCategory.SECURITY,
                title="No MFA",
                description="x",
            )
        ],
        compliant=False,
    )
    return results


def test_run_success_next_steps_when_clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No material findings ⇒ completed result with a next-steps document."""
    monkeypatch.setattr(pipeline, "load_context", lambda p: RepoContext(repo_path=str(p)))
    monkeypatch.setattr(pipeline, "run_all_criteria", lambda ctx: _clean_results())
    monkeypatch.setattr(
        pipeline,
        "write_report",
        lambda rp, tsc: (None, NextStepsDocument(title="Next Steps")),
    )

    out = SOC2AuditOrchestrator().run(tmp_path)
    assert isinstance(out, SOC2AuditResult)
    assert out.status == "completed"
    assert out.repo_path == str(tmp_path.resolve())
    assert len(out.tsc_results) == 5
    assert {r.category for r in out.tsc_results} == set(pipeline.TSC_CRITERIA)
    assert out.has_findings is False
    assert out.next_steps_document is not None
    assert out.compliance_report is None


def test_run_success_report_when_findings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Material findings ⇒ completed result with a compliance report."""
    monkeypatch.setattr(pipeline, "load_context", lambda p: RepoContext(repo_path=str(p)))
    monkeypatch.setattr(pipeline, "run_all_criteria", lambda ctx: _results_with_findings())
    monkeypatch.setattr(
        pipeline,
        "write_report",
        lambda rp, tsc: (SOC2ComplianceReport(executive_summary="s"), None),
    )

    out = SOC2AuditOrchestrator().run(tmp_path)
    assert out.status == "completed"
    assert out.has_findings is True
    assert out.compliance_report is not None
    assert out.next_steps_document is None


def test_run_repo_load_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_p):
        raise ValueError("bad repo")

    monkeypatch.setattr(pipeline, "load_context", _boom)
    out = SOC2AuditOrchestrator().run("/nonexistent")
    assert out.status == "failed"
    assert "bad repo" in (out.error or "")
    assert out.tsc_results == []


def test_run_pipeline_failure_returns_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(pipeline, "load_context", lambda p: RepoContext(repo_path=str(p)))

    def _boom(_ctx):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(pipeline, "run_all_criteria", _boom)
    out = SOC2AuditOrchestrator().run(tmp_path)
    assert out.status == "failed"
    assert "audit exploded" in (out.error or "")


def test_run_soc2_audit_module_wrapper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The module-level wrapper delegates to the orchestrator."""
    monkeypatch.setattr(pipeline, "load_context", lambda p: RepoContext(repo_path=str(p)))
    monkeypatch.setattr(pipeline, "run_all_criteria", lambda ctx: _clean_results())
    monkeypatch.setattr(
        pipeline, "write_report", lambda rp, tsc: (None, NextStepsDocument(title="Next"))
    )
    out = run_soc2_audit(tmp_path)
    assert isinstance(out, SOC2AuditResult)
    assert out.status == "completed"
