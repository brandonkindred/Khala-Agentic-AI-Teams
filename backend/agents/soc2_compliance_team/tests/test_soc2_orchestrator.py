"""Tests for SOC2 audit orchestrator and pipeline."""

from pathlib import Path
from unittest.mock import patch

import pytest

from llm_service import DummyLLMClient
from soc2_compliance_team.models import SOC2AuditResult, TSCCategory
from soc2_compliance_team.orchestrator import run_soc2_audit
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


def test_run_soc2_audit_dummy(tmp_path: Path) -> None:
    """Full audit with DummyLLM completes and returns next_steps when no findings."""
    (tmp_path / "app.py").write_text("# placeholder")
    dummy = DummyLLMClient()
    with patch("shared_graph.agent_factory.get_strands_model", return_value=dummy):
        result = run_soc2_audit(tmp_path)
    assert isinstance(result, SOC2AuditResult)
    assert result.status == "completed"
    assert result.repo_path == str(tmp_path.resolve())
    assert len(result.tsc_results) == 5
    categories = {r.category for r in result.tsc_results}
    assert categories == {
        TSCCategory.SECURITY,
        TSCCategory.AVAILABILITY,
        TSCCategory.PROCESSING_INTEGRITY,
        TSCCategory.CONFIDENTIALITY,
        TSCCategory.PRIVACY,
    }
    # Dummy returns no findings, so we get next_steps_document
    assert result.has_findings is False
    assert result.next_steps_document is not None
    assert result.next_steps_document.title
    assert result.compliance_report is None
