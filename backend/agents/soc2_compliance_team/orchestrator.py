"""Orchestrator for SOC2 compliance audit: load repo, run TSC agents, produce report or next-steps document."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from strands import Agent

from llm_service import get_strands_model

from .agents import (
    AvailabilityTSCAgent,
    ConfidentialityTSCAgent,
    PrivacyTSCAgent,
    ProcessingIntegrityTSCAgent,
    ReportWriterAgent,
    SecurityTSCAgent,
)
from .models import (
    FindingSeverity,
    NextStepsDocument,
    SOC2AuditResult,
    SOC2ComplianceReport,
    TSCAuditResult,
    TSCCategory,
)
from .repo_loader import load_repo_context

_NAME_TO_CATEGORY = {
    "Security": TSCCategory.SECURITY,
    "Availability": TSCCategory.AVAILABILITY,
    "Processing Integrity": TSCCategory.PROCESSING_INTEGRITY,
    "Confidentiality": TSCCategory.CONFIDENTIALITY,
    "Privacy": TSCCategory.PRIVACY,
}

logger = logging.getLogger(__name__)


class _StrandsLLMAdapter:
    """Adapts a Strands Agent to the LLMClient interface expected by SOC2 agents."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        result = self._agent(prompt)
        return str(result).strip()

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        raw = self.complete(prompt, temperature=temperature, system_prompt=system_prompt)
        return json.loads(raw)

    def get_max_context_tokens(self) -> int:
        return 16384


class SOC2AuditOrchestrator:
    """
    Runs a full SOC2 compliance audit on a code repository:
    1. Load repo context (code, config, docs)
    2. Run each TSC audit agent (Security, Availability, Processing Integrity, Confidentiality, Privacy)
    3. Compile results and produce either a compliance report (if issues found) or next-steps document
    """

    def __init__(self, llm_client=None) -> None:
        if llm_client is not None:
            self.llm = llm_client
        else:
            agent = Agent(model=get_strands_model("soc2"))
            self.llm = _StrandsLLMAdapter(agent)
        self.security_agent = SecurityTSCAgent()
        self.availability_agent = AvailabilityTSCAgent()
        self.processing_integrity_agent = ProcessingIntegrityTSCAgent()
        self.confidentiality_agent = ConfidentialityTSCAgent()
        self.privacy_agent = PrivacyTSCAgent()
        self.report_writer = ReportWriterAgent()

    def run(self, repo_path: str | Path) -> SOC2AuditResult:
        """
        Execute the full audit on the given repository path.
        Returns SOC2AuditResult with either compliance_report or next_steps_document set.
        """
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

        tsc_results: list[TSCAuditResult] = []

        agents = [
            ("Security", self.security_agent),
            ("Availability", self.availability_agent),
            ("Processing Integrity", self.processing_integrity_agent),
            ("Confidentiality", self.confidentiality_agent),
            ("Privacy", self.privacy_agent),
        ]

        for name, agent in agents:
            try:
                logger.info("Running %s TSC audit", name)
                result = agent.run(self.llm, context)
                tsc_results.append(result)
                logger.info(
                    "%s: %s findings, compliant=%s", name, len(result.findings), result.compliant
                )
            except Exception as e:
                logger.exception("TSC agent %s failed", name)
                tsc_results.append(
                    TSCAuditResult(
                        category=_NAME_TO_CATEGORY.get(name, TSCCategory.SECURITY),
                        summary=f"Audit failed: {e}",
                        findings=[],
                        compliant=False,
                    )
                )

        has_findings = any(
            not r.compliant
            or any(
                f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in r.findings
            )
            for r in tsc_results
        )

        compliance_report: SOC2ComplianceReport | None = None
        next_steps_document: NextStepsDocument | None = None

        try:
            compliance_report, next_steps_document = self.report_writer.run(
                self.llm, str(repo_path), tsc_results
            )
        except Exception as e:
            logger.exception("Report writer failed")
            return SOC2AuditResult(
                status="failed",
                repo_path=str(repo_path),
                tsc_results=tsc_results,
                has_findings=has_findings,
                error=f"Report generation failed: {e}",
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
    """
    One-shot SOC2 audit. Uses default Strands Agent if no LLM client provided.
    """
    orch = SOC2AuditOrchestrator(llm_client=llm_client)
    return orch.run(repo_path)
