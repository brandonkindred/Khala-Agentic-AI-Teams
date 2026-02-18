"""Orchestrator for SOC2 compliance audit: load repo, run TSC agents, produce report or next-steps document."""

from __future__ import annotations

import logging
from pathlib import Path

from .agents import (
    AvailabilityTSCAgent,
    ConfidentialityTSCAgent,
    ProcessingIntegrityTSCAgent,
    PrivacyTSCAgent,
    ReportWriterAgent,
    SecurityTSCAgent,
)
from .llm_client import LLMClient, get_llm_client
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


class SOC2AuditOrchestrator:
    """
    Runs a full SOC2 compliance audit on a code repository:
    1. Load repo context (code, config, docs)
    2. Run each TSC audit agent (Security, Availability, Processing Integrity, Confidentiality, Privacy)
    3. Compile results and produce either a compliance report (if issues found) or next-steps document
    """

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm = llm_client or get_llm_client()
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
                logger.info("%s: %s findings, compliant=%s", name, len(result.findings), result.compliant)
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
            or any(f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH) for f in r.findings)
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


def run_soc2_audit(repo_path: str | Path, llm_client: LLMClient | None = None) -> SOC2AuditResult:
    """
    One-shot SOC2 audit. Uses default LLM client if none provided.
    """
    orch = SOC2AuditOrchestrator(llm_client=llm_client)
    return orch.run(repo_path)
