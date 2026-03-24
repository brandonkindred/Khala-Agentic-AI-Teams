"""
Tool: audit.export_backlog

Export normalized findings for tracker ingestion.
"""

import json
from typing import List, Literal

from pydantic import BaseModel, Field

from ...models import Finding, PatternCluster, Severity


class ExportBacklogInput(BaseModel):
    """Input for exporting the findings backlog."""

    audit_id: str = Field(..., description="Audit identifier")
    findings: List[Finding] = Field(default_factory=list, description="Findings to export")
    patterns: List[PatternCluster] = Field(default_factory=list, description="Pattern clusters")
    format: Literal["json", "csv"] = Field(default="json", description="Export format")
    include_evidence_refs: bool = Field(default=True, description="Include evidence references")
    include_patterns: bool = Field(default=True, description="Include pattern clustering info")


class ExportBacklogOutput(BaseModel):
    """Output from exporting the backlog."""

    artifact_ref: str = Field(..., description="Reference to exported artifact")
    format: str
    counts: dict = Field(default_factory=dict)
    content: str = Field(default="", description="Exported content (for small exports)")


async def export_backlog(input_data: ExportBacklogInput) -> ExportBacklogOutput:
    """
    Export normalized findings backlog for tracker ingestion.

    Produces output suitable for import into Jira, Linear, GitHub Issues, etc.
    """
    findings = input_data.findings
    patterns = input_data.patterns

    # Count by severity
    counts = {
        "total": len(findings),
        "critical": sum(1 for f in findings if f.severity == Severity.CRITICAL),
        "high": sum(1 for f in findings if f.severity == Severity.HIGH),
        "medium": sum(1 for f in findings if f.severity == Severity.MEDIUM),
        "low": sum(1 for f in findings if f.severity == Severity.LOW),
        "patterns": len(patterns),
    }

    # Build export content
    if input_data.format == "json":
        export_data = {
            "audit_id": input_data.audit_id,
            "counts": counts,
            "findings": [],
            "patterns": [] if input_data.include_patterns else None,
        }

        for finding in findings:
            finding_dict = {
                "id": finding.id,
                "title": finding.title,
                "summary": finding.summary,
                "severity": finding.severity.value,
                "scope": finding.scope.value,
                "surface": finding.surface.value,
                "target": finding.target,
                "issue_type": finding.issue_type.value,
                "repro_steps": finding.repro_steps,
                "expected": finding.expected,
                "actual": finding.actual,
                "user_impact": finding.user_impact,
                "wcag_mappings": [
                    {"sc": m.sc, "name": m.name, "confidence": m.confidence}
                    for m in finding.wcag_mappings
                ],
                "section_508_tags": finding.section_508_tags,
                "root_cause_hypothesis": finding.root_cause_hypothesis,
                "recommended_fix": finding.recommended_fix,
                "acceptance_criteria": finding.acceptance_criteria,
                "test_plan": finding.test_plan,
            }

            if input_data.include_evidence_refs:
                finding_dict["evidence_pack_ref"] = finding.evidence_pack_ref

            if input_data.include_patterns:
                finding_dict["pattern_id"] = finding.pattern_id
                finding_dict["component_id"] = finding.component_id

            export_data["findings"].append(finding_dict)

        if input_data.include_patterns:
            for pattern in patterns:
                export_data["patterns"].append(
                    {
                        "pattern_id": pattern.pattern_id,
                        "name": pattern.name,
                        "description": pattern.description,
                        "severity": pattern.severity.value,
                        "scope": pattern.scope.value,
                        "linked_finding_ids": pattern.linked_finding_ids,
                        "fix_priority": pattern.fix_priority,
                    }
                )

        content = json.dumps(export_data, indent=2, default=str)

    else:  # CSV format
        lines = ["id,title,severity,scope,surface,target,issue_type,wcag_scs,user_impact"]
        for finding in findings:
            wcag_scs = ";".join(m.sc for m in finding.wcag_mappings)
            line = f'"{finding.id}","{finding.title}","{finding.severity.value}","{finding.scope.value}","{finding.surface.value}","{finding.target}","{finding.issue_type.value}","{wcag_scs}","{finding.user_impact}"'
            lines.append(line)
        content = "\n".join(lines)

    artifact_ref = f"backlog_{input_data.audit_id}.{input_data.format}"

    return ExportBacklogOutput(
        artifact_ref=artifact_ref,
        format=input_data.format,
        counts=counts,
        content=content,
    )
