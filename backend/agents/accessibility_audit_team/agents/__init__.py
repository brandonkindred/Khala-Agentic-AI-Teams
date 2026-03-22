"""
Specialist agents for the Digital Accessibility Audit Team.

8 Core Agents:
- APL (Accessibility Program Lead): Scope, strategy, coverage, final report
- WAS (Web Audit Specialist): Manual web testing + scan orchestration
- MAS (Mobile Accessibility Specialist): iOS/Android testing
- ATS (Assistive Technology Specialist): AT verification as truth layer
- SLMS (Standards & Legal Mapping Specialist): WCAG mapping + Section 508 tags
- REE (Reproduction & Evidence Engineer): Evidence bundles + minimal repros
- RA (Remediation Advisor): Fix guidance + acceptance criteria
- QCR (QA & Consistency Reviewer): Quality bar enforcement + dedupe
"""

from .assistive_tech_specialist import AssistiveTechSpecialist
from .evidence_engineer import EvidenceEngineer
from .mobile_accessibility_specialist import MobileAccessibilitySpecialist
from .program_lead import AccessibilityProgramLead
from .qa_consistency_reviewer import QAConsistencyReviewer
from .remediation_advisor import RemediationAdvisor
from .standards_mapping_specialist import StandardsMappingSpecialist
from .web_audit_specialist import WebAuditSpecialist

__all__ = [
    "AccessibilityProgramLead",
    "WebAuditSpecialist",
    "MobileAccessibilitySpecialist",
    "AssistiveTechSpecialist",
    "StandardsMappingSpecialist",
    "EvidenceEngineer",
    "RemediationAdvisor",
    "QAConsistencyReviewer",
]
