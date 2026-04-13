"""Orchestrator for SOC2 compliance audit: load repo, run TSC agents via Graph, produce report."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from shared_graph import extract_node_text, invoke_graph_sync

from .graphs.audit_graph import build_audit_graph
from .models import (
    FindingSeverity,
    NextStepsDocument,
    SOC2AuditResult,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
    TSCFinding,
)
from .repo_loader import load_repo_context

logger = logging.getLogger(__name__)

_TSC_NODE_TO_CATEGORY = {
    "security_tsc": TSCCategory.SECURITY,
    "availability_tsc": TSCCategory.AVAILABILITY,
    "processing_integrity_tsc": TSCCategory.PROCESSING_INTEGRITY,
    "confidentiality_tsc": TSCCategory.CONFIDENTIALITY,
    "privacy_tsc": TSCCategory.PRIVACY,
}


def _parse_tsc_result(text: str, category: TSCCategory) -> TSCAuditResult:
    """Parse a TSC agent's text output into a typed TSCAuditResult."""
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
        else:
            data = {}
    except (json.JSONDecodeError, ValueError):
        data = {}

    summary = data.get("summary") or ""
    findings_raw = data.get("findings") or []
    findings = []
    for f in findings_raw:
        if isinstance(f, dict) and (f.get("title") or f.get("description")):
            sev = (f.get("severity") or "medium").lower()
            try:
                severity = FindingSeverity(sev)
            except ValueError:
                severity = FindingSeverity.MEDIUM
            findings.append(
                TSCFinding(
                    severity=severity,
                    category=category,
                    title=f.get("title") or "Untitled",
                    description=f.get("description") or "",
                    location=f.get("location") or "",
                    recommendation=f.get("recommendation") or "",
                    evidence_observed=f.get("evidence_observed") or "",
                )
            )

    compliant = data.get(
        "compliant",
        len([f for f in findings if f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)]) == 0,
    )
    return TSCAuditResult(category=category, summary=summary, findings=findings, compliant=compliant)


def _parse_report_output(
    text: str, repo_path: str, tsc_results: list[TSCAuditResult]
) -> tuple[SOC2ComplianceReport | None, NextStepsDocument | None]:
    """Parse the report writer agent's text output into typed models."""
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
        else:
            data = {}
    except (json.JSONDecodeError, ValueError):
        data = {}

    report_type = data.get("report_type", "")

    if report_type == "next_steps":
        steps = data.get("steps") or []
        if not isinstance(steps, list):
            steps = []
        return None, NextStepsDocument(
            title=data.get("title") or "Next Steps for SOC2 Certification",
            introduction=data.get("introduction") or "",
            steps=[s if isinstance(s, dict) else {"title": str(s), "description": ""} for s in steps],
            recommended_timeline=data.get("recommended_timeline") or "",
            raw_markdown=data.get("raw_markdown") or "",
        )

    # compliance_audit report (default when findings exist)
    findings_by_tsc: Dict[str, list[TSCFinding]] = {}
    raw_findings = data.get("findings_by_tsc") or {}
    for cat, list_dicts in raw_findings.items():
        try:
            findings_by_tsc[cat] = [TSCFinding(**d) for d in list_dicts]
        except Exception:
            findings_by_tsc[cat] = []

    # Fall back to structured TSC results if the report writer didn't
    # produce usable findings_by_tsc (empty dict OR dict with all-empty lists)
    has_any_findings = any(v for v in findings_by_tsc.values())
    if not has_any_findings:
        for r in tsc_results:
            if r.findings:
                findings_by_tsc[r.category.value] = r.findings

    return SOC2ComplianceReport(
        executive_summary=data.get("executive_summary") or "",
        scope=data.get("scope") or f"Repository: {repo_path}",
        findings_by_tsc=findings_by_tsc,
        recommendations_summary=data.get("recommendations_summary") or [],
        raw_markdown=data.get("raw_markdown") or "",
    ), None


class SOC2AuditOrchestrator:
    """Runs a full SOC2 compliance audit via a Strands fan-out/fan-in Graph.

    Five TSC specialist agents run in parallel, then a report writer
    synthesizes all findings into either a compliance report or a
    next-steps certification document.
    """

    def run(self, repo_path: str | Path) -> SOC2AuditResult:
        """Execute the full audit on the given repository path."""
        repo_path = Path(repo_path).resolve()
        logger.info("SOC2 audit starting for repo: %s", repo_path)

        try:
            context = load_repo_context(repo_path)
        except Exception as e:
            logger.exception("Failed to load repo context")
            return SOC2AuditResult(
                status="failed",
                repo_path=str(repo_path),
                tsc_results=[],
                has_findings=False,
                error=str(e),
            )

        # Build task string with serialized repo context
        task = (
            f"Audit the following repository for SOC2 compliance.\n\n"
            f"Repository path: {context.repo_path}\n"
            f"Tech stack: {context.tech_stack_hint}\n"
            f"Files scanned: {context.file_list}\n\n"
            f"README/docs:\n```\n{context.readme_content}\n```\n\n"
            f"Code and configuration:\n```\n{context.code_summary}\n```"
        )

        # Build and invoke the fan-out/fan-in graph
        graph = build_audit_graph()

        try:
            result = invoke_graph_sync(graph, task)
        except Exception as e:
            logger.exception("Graph execution failed")
            return SOC2AuditResult(
                status="failed",
                repo_path=str(repo_path),
                tsc_results=[],
                has_findings=False,
                error=f"Graph execution failed: {e}",
            )

        # Extract TSC results from each parallel node
        tsc_results: list[TSCAuditResult] = []
        for node_id, category in _TSC_NODE_TO_CATEGORY.items():
            text = extract_node_text(result, node_id)
            if text:
                tsc_results.append(_parse_tsc_result(text, category))
            else:
                logger.warning("No output from TSC node %s", node_id)
                tsc_results.append(
                    TSCAuditResult(
                        category=category,
                        summary=f"Audit node {node_id} produced no output",
                        findings=[],
                        compliant=True,
                    )
                )

        has_findings = any(
            not r.compliant
            or any(f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in r.findings)
            for r in tsc_results
        )

        # Extract report from compositor node
        report_text = extract_node_text(result, "report_writer")
        compliance_report: SOC2ComplianceReport | None = None
        next_steps_document: NextStepsDocument | None = None

        if report_text:
            try:
                compliance_report, next_steps_document = _parse_report_output(
                    report_text, str(repo_path), tsc_results
                )
                # Ensure correct output type based on findings
                if has_findings and compliance_report is None:
                    compliance_report, next_steps_document = _parse_report_output(
                        report_text, str(repo_path), tsc_results
                    )
                elif not has_findings and next_steps_document is None:
                    next_steps_document = NextStepsDocument(
                        title="Next Steps for SOC2 Certification",
                        introduction="The codebase audit found no material SOC2 compliance gaps.",
                        steps=[],
                        recommended_timeline="",
                        raw_markdown=report_text,
                    )
            except Exception:
                logger.exception("Report parsing failed")
        else:
            logger.warning("Report writer produced no output")
            if not has_findings:
                next_steps_document = NextStepsDocument(
                    title="Next Steps for SOC2 Certification",
                    introduction="The codebase audit found no material SOC2 compliance gaps.",
                    steps=[],
                    recommended_timeline="",
                    raw_markdown="",
                )

        return SOC2AuditResult(
            status="completed",
            repo_path=str(repo_path),
            tsc_results=tsc_results,
            has_findings=has_findings,
            compliance_report=compliance_report,
            next_steps_document=next_steps_document,
        )


def run_soc2_audit(repo_path: str | Path, llm_client=None) -> SOC2AuditResult:
    """One-shot SOC2 audit via Strands Graph."""
    orch = SOC2AuditOrchestrator()
    return orch.run(repo_path)
