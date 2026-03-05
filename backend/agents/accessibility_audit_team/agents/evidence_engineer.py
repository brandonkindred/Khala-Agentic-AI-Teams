"""
Reproduction & Evidence Engineer (REE)

Owns: Proof bundles and reproducibility
Outputs: Evidence pack + minimal repro when feasible
"""

from typing import Any, Dict, List, Optional

from .base import AgentMessage, BaseSpecialistAgent
from ..models import EvidencePack, Finding, Phase
from ..tools.evidence import create_pack, generate_minimal_case
from ..tools.evidence.create_pack import (
    CreatePackInput,
    ArtifactInput,
    EnvironmentInput,
)
from ..tools.evidence.generate_minimal_case import GenerateMinimalCaseInput


class EvidenceEngineer(BaseSpecialistAgent):
    """
    Reproduction & Evidence Engineer (REE).

    The REE ensures findings are credible and reproducible:
    - Capture screenshots and/or video with environment metadata
    - Capture DOM and computed styles excerpt (web)
    - Capture a11y tree excerpts (web/mobile where possible)
    - Collect logs if relevant (console warnings for ARIA errors, etc.)
    - Produce minimal repro snippet for systemic bugs when possible

    A finding WITHOUT evidence is NOT reportable.
    """

    agent_code = "REE"
    agent_name = "Reproduction & Evidence Engineer"
    description = "Proof bundles and reproducibility"

    async def process(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process an REE task based on the current phase.

        Phases handled:
        - DISCOVERY: Capture evidence for draft findings
        - VERIFICATION: Supplement evidence for verified findings
        """
        phase = context.get("phase", Phase.DISCOVERY)
        audit_id = context.get("audit_id", "")

        if phase in [Phase.DISCOVERY, Phase.VERIFICATION]:
            return await self._handle_evidence_capture(context)
        else:
            return {"success": False, "error": f"REE does not handle phase {phase}"}

    async def _handle_evidence_capture(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Capture evidence for findings.
        """
        audit_id = context.get("audit_id", "")
        findings: List[Finding] = context.get("findings", [])

        if not findings:
            return {"success": True, "evidence_packs": []}

        evidence_packs: List[EvidencePack] = []
        minimal_repros: Dict[str, str] = {}

        for finding in findings:
            # Create evidence pack
            artifacts = self._generate_artifacts_for_finding(finding)
            environment = self._get_environment_for_finding(finding)

            pack_input = CreatePackInput(
                audit_id=audit_id,
                finding_id=finding.id,
                artifacts=artifacts,
                environment=environment,
                notes=f"Evidence for {finding.issue_type.value} issue",
            )

            pack_output = await create_pack(pack_input)
            evidence_packs.append(pack_output.evidence_pack)

            # Update finding with evidence reference
            finding.evidence_pack_ref = pack_output.pack_ref

            # Try to generate minimal repro for systemic issues
            if finding.scope.value == "Systemic" and finding.surface.value == "web":
                repro_input = GenerateMinimalCaseInput(
                    audit_id=audit_id,
                    finding_id=finding.id,
                    goal=f"Demonstrate {finding.issue_type.value} issue",
                    include_styles=True,
                    include_scripts=False,
                )

                repro_output = await generate_minimal_case(repro_input)

                if repro_output.confidence > 0.5:
                    minimal_repros[finding.id] = repro_output.snippet_ref

        return {
            "success": True,
            "phase": context.get("phase"),
            "evidence_packs": evidence_packs,
            "minimal_repros": minimal_repros,
            "findings_with_evidence": len(evidence_packs),
        }

    def _generate_artifacts_for_finding(self, finding: Finding) -> List[ArtifactInput]:
        """Generate artifact list based on finding type."""
        artifacts = []

        # Always include a screenshot
        artifacts.append(
            ArtifactInput(
                artifact_type="screenshot",
                content_ref=f"screenshot_{finding.id}",
                description=f"Screenshot showing {finding.issue_type.value} issue",
            )
        )

        # Add type-specific artifacts
        issue_type = finding.issue_type.value

        if issue_type in ["name_role_value", "structure", "forms"]:
            artifacts.append(
                ArtifactInput(
                    artifact_type="dom_snapshot",
                    content_ref=f"dom_{finding.id}",
                    description="DOM structure showing the issue",
                )
            )
            artifacts.append(
                ArtifactInput(
                    artifact_type="a11y_tree",
                    content_ref=f"a11y_tree_{finding.id}",
                    description="Accessibility tree excerpt",
                )
            )

        if issue_type in ["keyboard", "focus", "navigation"]:
            artifacts.append(
                ArtifactInput(
                    artifact_type="video",
                    content_ref=f"video_{finding.id}",
                    description="Screen recording demonstrating the issue",
                )
            )

        if issue_type == "contrast":
            artifacts.append(
                ArtifactInput(
                    artifact_type="log",
                    content_ref=f"contrast_calc_{finding.id}",
                    description="Contrast calculation details",
                )
            )

        return artifacts

    def _get_environment_for_finding(self, finding: Finding) -> EnvironmentInput:
        """Get environment information based on finding surface."""
        surface = finding.surface.value

        if surface == "web":
            return EnvironmentInput(
                surface="web",
                browser_or_device="Chrome 120",
                os_version="Windows 11",
                viewport_or_scale="1920x1080",
                assistive_tech="NVDA 2024.1",
            )
        elif surface == "ios":
            return EnvironmentInput(
                surface="ios",
                browser_or_device="iPhone 15",
                os_version="iOS 17.2",
                viewport_or_scale="Default",
                assistive_tech="VoiceOver",
            )
        elif surface == "android":
            return EnvironmentInput(
                surface="android",
                browser_or_device="Pixel 8",
                os_version="Android 14",
                viewport_or_scale="Default",
                assistive_tech="TalkBack",
            )
        else:
            return EnvironmentInput(surface=surface)

    async def capture_additional_evidence(
        self,
        audit_id: str,
        finding_id: str,
        artifact_types: List[str],
    ) -> Dict[str, Any]:
        """
        Capture additional evidence artifacts for a finding.

        Used when QCR requests more evidence before approving.
        """
        artifacts = [
            ArtifactInput(
                artifact_type=art_type,
                content_ref=f"{art_type}_{finding_id}_supplemental",
                description=f"Supplemental {art_type} evidence",
            )
            for art_type in artifact_types
        ]

        pack_input = CreatePackInput(
            audit_id=audit_id,
            finding_id=finding_id,
            artifacts=artifacts,
            environment=EnvironmentInput(surface="web"),
            notes="Supplemental evidence per QCR request",
        )

        pack_output = await create_pack(pack_input)

        return {
            "success": True,
            "pack_ref": pack_output.pack_ref,
            "artifacts_added": len(artifacts),
        }
