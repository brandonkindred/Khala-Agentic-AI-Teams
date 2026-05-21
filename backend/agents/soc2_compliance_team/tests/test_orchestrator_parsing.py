"""Targeted tests for the SOC2 orchestrator's parsing helpers and the
``SOC2AuditOrchestrator.run`` orchestration paths (success, repo-load
failure, graph-invoke failure, no-output, malformed report writer
output, etc.).

The Strands ``Graph`` invocation is fully mocked so these tests do not
depend on any LLM or external service.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from soc2_compliance_team.models import (
    FindingSeverity,
    NextStepsDocument,
    SOC2AuditResult,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
    TSCFinding,
)
from soc2_compliance_team.orchestrator import (
    SOC2AuditOrchestrator,
    _parse_report_output,
    _parse_tsc_result,
    run_soc2_audit,
)

# ---------------------------------------------------------------------------
# _parse_tsc_result
# ---------------------------------------------------------------------------


def test_parse_tsc_result_handles_valid_json() -> None:
    text = json.dumps(
        {
            "summary": "S",
            "findings": [
                {
                    "severity": "critical",
                    "title": "Hardcoded secret",
                    "description": "Found AWS key in repo",
                    "location": "config.py",
                    "recommendation": "Rotate + use Vault",
                    "evidence_observed": "AKIA...",
                },
                {"severity": "junk", "title": "Bad sev"},
            ],
            "compliant": False,
        }
    )
    out = _parse_tsc_result(text, TSCCategory.SECURITY)
    assert isinstance(out, TSCAuditResult)
    assert out.summary == "S"
    assert len(out.findings) == 2
    assert out.findings[0].severity is FindingSeverity.CRITICAL
    # Invalid severity falls back to MEDIUM
    assert out.findings[1].severity is FindingSeverity.MEDIUM
    assert out.compliant is False


def test_parse_tsc_result_with_prose_around_json() -> None:
    text = "Here is the audit:\n" + json.dumps({"summary": "x", "findings": []}) + "\nEnd."
    out = _parse_tsc_result(text, TSCCategory.PRIVACY)
    assert out.summary == "x"
    assert out.findings == []
    assert out.compliant is True


def test_parse_tsc_result_handles_invalid_json_gracefully() -> None:
    # Braces are present but the slice is not valid JSON →
    # json.loads raises JSONDecodeError, which the except branch catches.
    out = _parse_tsc_result("garbage {not really: 'json'}", TSCCategory.AVAILABILITY)
    assert out.summary == ""
    assert out.findings == []
    assert out.compliant is True


def test_parse_tsc_result_handles_no_braces() -> None:
    out = _parse_tsc_result("plain text", TSCCategory.AVAILABILITY)
    assert out.summary == ""
    assert out.findings == []


def test_parse_tsc_result_default_compliant_from_critical_finding() -> None:
    """When `compliant` is missing and there is a high/critical finding,
    the helper should default to False."""
    text = json.dumps(
        {
            "summary": "s",
            "findings": [{"severity": "high", "title": "x"}],
        }
    )
    out = _parse_tsc_result(text, TSCCategory.SECURITY)
    assert out.compliant is False


def test_parse_tsc_result_skips_non_dict_and_blank_findings() -> None:
    text = json.dumps(
        {
            "summary": "",
            "findings": [
                "not-a-dict",
                {"severity": "low"},  # title + description blank
                {"severity": "low", "title": "real"},
            ],
        }
    )
    out = _parse_tsc_result(text, TSCCategory.CONFIDENTIALITY)
    assert len(out.findings) == 1
    assert out.findings[0].title == "real"


# ---------------------------------------------------------------------------
# _parse_report_output
# ---------------------------------------------------------------------------


def test_parse_report_output_next_steps_full() -> None:
    text = json.dumps(
        {
            "report_type": "next_steps",
            "title": "Next Steps",
            "introduction": "i",
            "steps": [
                {"title": "Engage CPA", "description": "..."},
                "string-becomes-stub",
            ],
            "recommended_timeline": "6mo",
            "raw_markdown": "# md",
        }
    )
    report, next_steps = _parse_report_output(text, "/r", [])
    assert report is None
    assert isinstance(next_steps, NextStepsDocument)
    assert next_steps.title == "Next Steps"
    assert any(s.get("title") == "string-becomes-stub" for s in next_steps.steps)


def test_parse_report_output_next_steps_with_non_list_steps() -> None:
    text = json.dumps(
        {
            "report_type": "next_steps",
            "title": "",
            "steps": "not-a-list",
        }
    )
    _, next_steps = _parse_report_output(text, "/r", [])
    assert next_steps is not None
    assert next_steps.steps == []
    # Default title kicks in when LLM returned blank
    assert next_steps.title == "Next Steps for SOC2 Certification"


def test_parse_report_output_compliance_audit_with_valid_findings() -> None:
    text = json.dumps(
        {
            "report_type": "compliance_audit",
            "executive_summary": "es",
            "scope": "sc",
            "findings_by_tsc": {
                "security": [
                    {
                        "severity": "critical",
                        "category": "security",
                        "title": "Bad",
                        "description": "D",
                        "location": "L",
                        "recommendation": "R",
                        "evidence_observed": "E",
                    }
                ],
            },
            "recommendations_summary": ["fix it"],
            "raw_markdown": "# r",
        }
    )
    tsc_results = [
        TSCAuditResult(
            category=TSCCategory.SECURITY,
            summary="s",
            findings=[
                TSCFinding(
                    severity=FindingSeverity.CRITICAL,
                    category=TSCCategory.SECURITY,
                    title="t",
                    description="d",
                )
            ],
            compliant=False,
        )
    ]
    report, next_steps = _parse_report_output(text, "/r", tsc_results)
    assert next_steps is None
    assert isinstance(report, SOC2ComplianceReport)
    assert report.executive_summary == "es"
    assert report.scope == "sc"
    assert len(report.findings_by_tsc["security"]) == 1


def test_parse_report_output_falls_back_to_tsc_findings_when_none_in_report() -> None:
    """If the LLM didn't populate findings_by_tsc but the per-TSC results
    have findings, the helper should backfill from those."""
    text = json.dumps(
        {
            "report_type": "compliance_audit",
            "executive_summary": "es",
            "findings_by_tsc": {},
        }
    )
    tsc_results = [
        TSCAuditResult(
            category=TSCCategory.SECURITY,
            findings=[
                TSCFinding(
                    severity=FindingSeverity.HIGH,
                    category=TSCCategory.SECURITY,
                    title="X",
                    description="d",
                )
            ],
            compliant=False,
        )
    ]
    report, _ = _parse_report_output(text, "/repo/path", tsc_results)
    assert report is not None
    assert "security" in report.findings_by_tsc
    assert len(report.findings_by_tsc["security"]) == 1
    # Default scope falls back to "Repository: {path}"
    assert "/repo/path" in report.scope


def test_parse_report_output_invalid_findings_dict_swallowed() -> None:
    """If a per-category list contains dicts that can't be coerced into
    TSCFinding, the exception is caught and that category gets [].
    """
    text = json.dumps(
        {
            "report_type": "compliance_audit",
            "executive_summary": "es",
            "findings_by_tsc": {
                # Missing required `description` ⇒ ValidationError on construction
                "security": [{"missing_required_field": True}],
            },
        }
    )
    report, _ = _parse_report_output(text, "/r", [])
    assert report is not None
    # The except branch fires, category coerced to []
    assert report.findings_by_tsc["security"] == []


def test_parse_report_output_no_json_at_all() -> None:
    report, _ = _parse_report_output("no json here", "/r", [])
    # Defaults to compliance_audit branch
    assert report is not None
    assert report.executive_summary == ""


def test_parse_report_output_invalid_json_string() -> None:
    # `{...}` matches but the body is not valid JSON → exception path.
    report, _ = _parse_report_output("prefix {bogus: stuff}", "/r", [])
    assert report is not None
    assert report.executive_summary == ""


# ---------------------------------------------------------------------------
# SOC2AuditOrchestrator.run — integration paths (Graph fully mocked)
# ---------------------------------------------------------------------------


class _FakeNodeResult:
    """Walks like a Strands node result for our extractor."""

    def __init__(self, text: str) -> None:
        self._text = text
        # Mirror the duck-typed `node_result.result` attribute access used
        # by extract_node_text.
        self.result = self

    def get_agent_results(self) -> list[Any]:
        class _Agent:
            def __init__(self, t: str) -> None:
                self.message = {"content": [{"text": t}]}

        return [_Agent(self._text)]


class _FakeGraphResult:
    """Top-level fake result wrapping a dict of node_id -> _FakeNodeResult."""

    def __init__(self, nodes: Dict[str, _FakeNodeResult]) -> None:
        self.result = nodes


def _build_graph_result_with_findings() -> _FakeGraphResult:
    """A graph result where Security reports a critical finding and the
    report writer emits a compliance_audit JSON."""
    tsc_outputs = {
        "security_tsc": _FakeNodeResult(
            json.dumps(
                {
                    "summary": "weak",
                    "findings": [
                        {
                            "severity": "critical",
                            "title": "No auth",
                            "description": "No authentication implemented",
                            "location": "main.py",
                            "recommendation": "Add OAuth",
                            "evidence_observed": "no auth code",
                        }
                    ],
                    "compliant": False,
                }
            )
        ),
        "availability_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "processing_integrity_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "confidentiality_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "privacy_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "report_writer": _FakeNodeResult(
            json.dumps(
                {
                    "report_type": "compliance_audit",
                    "executive_summary": "exec",
                    "scope": "scope",
                    "findings_by_tsc": {
                        "security": [
                            {
                                "severity": "critical",
                                "category": "security",
                                "title": "No auth",
                                "description": "d",
                                "location": "L",
                                "recommendation": "R",
                                "evidence_observed": "E",
                            }
                        ]
                    },
                    "recommendations_summary": ["fix it"],
                    "raw_markdown": "# r",
                }
            )
        ),
    }
    return _FakeGraphResult(tsc_outputs)


def _build_graph_result_no_findings(report_type: str = "next_steps") -> _FakeGraphResult:
    nodes = {
        node_id: _FakeNodeResult(json.dumps({"summary": "ok", "findings": [], "compliant": True}))
        for node_id in (
            "security_tsc",
            "availability_tsc",
            "processing_integrity_tsc",
            "confidentiality_tsc",
            "privacy_tsc",
        )
    }
    if report_type == "next_steps":
        nodes["report_writer"] = _FakeNodeResult(
            json.dumps(
                {
                    "report_type": "next_steps",
                    "title": "NS",
                    "introduction": "i",
                    "steps": [{"title": "step", "description": "..."}],
                    "recommended_timeline": "6mo",
                    "raw_markdown": "# md",
                }
            )
        )
    elif report_type == "missing":
        # No report_writer node at all → triggers the "no report text" branch
        pass
    elif report_type == "empty_str":
        # Empty content → extract_node_text returns ""
        nodes["report_writer"] = _FakeNodeResult("")
    elif report_type == "mismatch":
        # Compliance_audit JSON when there are no findings → triggers
        # the "next_steps_document is None" rebuild branch in run()
        nodes["report_writer"] = _FakeNodeResult(
            json.dumps(
                {
                    "report_type": "compliance_audit",
                    "executive_summary": "es",
                }
            )
        )
    elif report_type == "raises":
        nodes["report_writer"] = _FakeNodeResult("just-text-no-json")
    return _FakeGraphResult(nodes)


def test_orchestrator_run_handles_repo_load_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If load_repo_context raises, orchestrator returns a failed result."""
    import soc2_compliance_team.orchestrator as omod

    def _boom(_: Any) -> Any:
        raise RuntimeError("disk gone")

    monkeypatch.setattr(omod, "load_repo_context", _boom)
    out = SOC2AuditOrchestrator().run("/nonexistent")
    assert isinstance(out, SOC2AuditResult)
    assert out.status == "failed"
    assert out.error and "disk gone" in out.error
    assert out.tsc_results == []
    assert out.has_findings is False


def test_orchestrator_run_handles_graph_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If invoke_graph_sync raises, orchestrator returns a failed result."""
    (tmp_path / "x.py").write_text("a=1")
    import soc2_compliance_team.orchestrator as omod

    def _boom(_graph, _task):
        raise RuntimeError("graph kaboom")

    monkeypatch.setattr(omod, "invoke_graph_sync", _boom)
    out = SOC2AuditOrchestrator().run(tmp_path)
    assert out.status == "failed"
    assert out.error and "graph kaboom" in out.error


def test_orchestrator_run_happy_path_with_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("a=1")
    import soc2_compliance_team.orchestrator as omod

    monkeypatch.setattr(omod, "invoke_graph_sync", lambda g, t: _build_graph_result_with_findings())
    out = SOC2AuditOrchestrator().run(tmp_path)

    assert out.status == "completed"
    assert out.has_findings is True
    assert out.compliance_report is not None
    assert out.next_steps_document is None
    assert len(out.tsc_results) == 5
    sec = next(r for r in out.tsc_results if r.category is TSCCategory.SECURITY)
    assert any(f.severity is FindingSeverity.CRITICAL for f in sec.findings)


def test_orchestrator_run_happy_path_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "main.py").write_text("a=1")
    import soc2_compliance_team.orchestrator as omod

    monkeypatch.setattr(
        omod, "invoke_graph_sync", lambda g, t: _build_graph_result_no_findings("next_steps")
    )
    out = SOC2AuditOrchestrator().run(tmp_path)

    assert out.status == "completed"
    assert out.has_findings is False
    assert out.next_steps_document is not None
    assert out.compliance_report is None
    assert out.next_steps_document.title == "NS"


def test_orchestrator_run_missing_report_writer_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the report_writer node is missing but there are no findings,
    the orchestrator should synthesise a NextStepsDocument."""
    (tmp_path / "main.py").write_text("a=1")
    import soc2_compliance_team.orchestrator as omod

    monkeypatch.setattr(
        omod, "invoke_graph_sync", lambda g, t: _build_graph_result_no_findings("missing")
    )
    out = SOC2AuditOrchestrator().run(tmp_path)

    assert out.status == "completed"
    assert out.has_findings is False
    assert out.next_steps_document is not None
    assert out.next_steps_document.title == "Next Steps for SOC2 Certification"
    assert out.compliance_report is None


def test_orchestrator_run_findings_but_writer_returns_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Findings exist but report writer emits a next_steps document — the
    orchestrator should re-parse to attempt to recover a compliance report
    (which on identical input yields the same outcome but still exercises
    line 215)."""
    (tmp_path / "main.py").write_text("a=1")
    import soc2_compliance_team.orchestrator as omod

    # Security has a critical finding → has_findings True. But report writer
    # emits a next_steps response → compliance_report is None on first parse.
    tsc_text = json.dumps(
        {
            "summary": "weak",
            "findings": [
                {
                    "severity": "critical",
                    "title": "No auth",
                    "description": "missing",
                }
            ],
            "compliant": False,
        }
    )
    ok_text = json.dumps({"summary": "ok", "findings": [], "compliant": True})
    nodes = {
        "security_tsc": _FakeNodeResult(tsc_text),
        "availability_tsc": _FakeNodeResult(ok_text),
        "processing_integrity_tsc": _FakeNodeResult(ok_text),
        "confidentiality_tsc": _FakeNodeResult(ok_text),
        "privacy_tsc": _FakeNodeResult(ok_text),
        "report_writer": _FakeNodeResult(
            json.dumps(
                {
                    "report_type": "next_steps",
                    "title": "NS",
                    "introduction": "",
                    "steps": [],
                    "recommended_timeline": "",
                    "raw_markdown": "",
                }
            )
        ),
    }
    fake_result = _FakeGraphResult(nodes)
    monkeypatch.setattr(omod, "invoke_graph_sync", lambda g, t: fake_result)

    out = SOC2AuditOrchestrator().run(tmp_path)
    assert out.status == "completed"
    assert out.has_findings is True
    # Even after re-parse, the writer-supplied next_steps remains; compliance
    # report stays None. The point is line 215 was executed.
    assert out.compliance_report is None


def test_orchestrator_run_mismatched_report_type_rebuilds_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the report writer returns a compliance_audit but there are no
    findings, the orchestrator should fall back to a next-steps document."""
    (tmp_path / "x.py").write_text("a")
    import soc2_compliance_team.orchestrator as omod

    monkeypatch.setattr(
        omod, "invoke_graph_sync", lambda g, t: _build_graph_result_no_findings("mismatch")
    )
    out = SOC2AuditOrchestrator().run(tmp_path)
    assert out.status == "completed"
    assert out.has_findings is False
    assert out.next_steps_document is not None


def test_orchestrator_run_skips_missing_tsc_nodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a TSC node produces no text, a placeholder TSCAuditResult is added."""
    (tmp_path / "x.py").write_text("a")
    import soc2_compliance_team.orchestrator as omod

    nodes = {
        "security_tsc": _FakeNodeResult(""),  # blank
        "availability_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "processing_integrity_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "confidentiality_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "privacy_tsc": _FakeNodeResult(
            json.dumps({"summary": "ok", "findings": [], "compliant": True})
        ),
        "report_writer": _FakeNodeResult(""),
    }
    fake_result = _FakeGraphResult(nodes)
    monkeypatch.setattr(omod, "invoke_graph_sync", lambda g, t: fake_result)

    out = SOC2AuditOrchestrator().run(tmp_path)
    assert len(out.tsc_results) == 5
    sec = next(r for r in out.tsc_results if r.category is TSCCategory.SECURITY)
    # The blank-output placeholder summary contains the node id
    assert "security_tsc" in sec.summary


def test_orchestrator_run_report_parse_exception_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If _parse_report_output raises, the orchestrator should log and
    continue without crashing."""
    (tmp_path / "x.py").write_text("a")
    import soc2_compliance_team.orchestrator as omod

    monkeypatch.setattr(
        omod, "invoke_graph_sync", lambda g, t: _build_graph_result_no_findings("next_steps")
    )

    def _explode(*a: Any, **k: Any) -> Any:
        raise RuntimeError("parse failed")

    monkeypatch.setattr(omod, "_parse_report_output", _explode)
    out = SOC2AuditOrchestrator().run(tmp_path)
    assert out.status == "completed"
    assert out.has_findings is False
    # Parse failed → no report, no next steps were produced from the writer,
    # but the orchestrator has no fallback when both report_text exists and
    # parsing fails → both remain None and the call still returns cleanly.
    assert out.compliance_report is None


def test_run_soc2_audit_module_level_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The module-level helper just instantiates the orchestrator and runs."""
    (tmp_path / "main.py").write_text("a=1")
    import soc2_compliance_team.orchestrator as omod

    captured: Dict[str, Any] = {}

    class _FakeOrch:
        def run(self, repo_path):
            captured["repo"] = str(repo_path)
            return SOC2AuditResult(status="completed", repo_path=str(repo_path))

    monkeypatch.setattr(omod, "SOC2AuditOrchestrator", _FakeOrch)
    out = run_soc2_audit(tmp_path, llm_client=object())
    assert out.status == "completed"
    assert captured["repo"] == str(tmp_path)
