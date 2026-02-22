"""CI/CD lint and validation tool agent."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import yaml
from pydantic import BaseModel, Field


class CICDLintInput(BaseModel):
    repo_path: str


class CICDLintOutput(BaseModel):
    success: bool
    checks: Dict[str, str] = Field(default_factory=dict)
    findings: List[str] = Field(default_factory=list)


class CICDLintPipelineValidationToolAgent:
    """Validates workflow syntax and required gates presence."""

    def run(self, input_data: CICDLintInput) -> CICDLintOutput:
        root = Path(input_data.repo_path).resolve()
        checks: Dict[str, str] = {
            "pipeline_lint": "skipped",
            "pipeline_gate_check": "skipped",
        }
        findings: List[str] = []

        workflows = list((root / ".github" / "workflows").glob("*.y*ml"))
        if not workflows:
            return CICDLintOutput(success=True, checks=checks, findings=findings)

        checks["pipeline_lint"] = "pass"
        checks["pipeline_gate_check"] = "pass"
        for wf in workflows:
            try:
                content = wf.read_text(encoding="utf-8", errors="replace")
                data = yaml.safe_load(content)
                if not isinstance(data, dict) or "jobs" not in data:
                    checks["pipeline_lint"] = "fail"
                    findings.append(f"{wf.name}: missing jobs")
                    continue
                wf_text = content.lower()
                if "deploy" in wf_text and "production" in wf_text and "approval" not in wf_text:
                    checks["pipeline_gate_check"] = "fail"
                    findings.append(f"{wf.name}: production deploy appears to miss approval gate")
            except Exception as exc:
                checks["pipeline_lint"] = "fail"
                findings.append(f"{wf.name}: parse error: {exc}")

        success = not any(v == "fail" for v in checks.values())
        return CICDLintOutput(success=success, checks=checks, findings=findings)
