"""
Domain models for the Digital Accessibility Audit Team.

Defines phases, taxonomy enums, findings, audit plans, evidence packs,
and all result types for the accessibility audit workflow.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Phase Enum
# ---------------------------------------------------------------------------


class Phase(str, Enum):
    """Lifecycle phases of the accessibility audit workflow."""

    INTAKE = "intake"
    DISCOVERY = "discovery"
    VERIFICATION = "verification"
    REPORT_PACKAGING = "report_packaging"
    RETEST = "retest"


# ---------------------------------------------------------------------------
# Taxonomy Enums
# ---------------------------------------------------------------------------


class Surface(str, Enum):
    """Platform surface being audited."""

    WEB = "web"
    IOS = "ios"
    ANDROID = "android"
    PDF = "pdf"


class Severity(str, Enum):
    """Finding severity based on impact, frequency, and workaround availability."""

    CRITICAL = "Critical"  # Blocks core task, no workaround
    HIGH = "High"  # Major friction, abandonment risk
    MEDIUM = "Medium"  # Meaningful friction, workaround exists
    LOW = "Low"  # Minor friction, not urgent


class Scope(str, Enum):
    """How widespread the issue is across the audited surface."""

    SYSTEMIC = "Systemic"  # Present across entire system/component library
    MULTI_AREA = "Multi-area"  # Present in multiple distinct areas
    LOCALIZED = "Localized"  # Isolated to a single location


class FindingState(str, Enum):
    """State machine for finding lifecycle."""

    DRAFT = "draft"
    NEEDS_VERIFICATION = "needs_verification"
    VERIFIED = "verified"
    READY_FOR_REPORT = "ready_for_report"
    CLOSED = "closed"


class IssueType(str, Enum):
    """Accessibility issue type classification."""

    NAME_ROLE_VALUE = "name_role_value"
    KEYBOARD = "keyboard"
    FOCUS = "focus"
    FORMS = "forms"
    CONTRAST = "contrast"
    STRUCTURE = "structure"
    TIMING = "timing"
    MEDIA = "media"
    MOTION = "motion"
    INPUT_MODALITY = "input_modality"
    ERROR_HANDLING = "error_handling"
    NAVIGATION = "navigation"
    RESIZING_REFLOW = "resizing_reflow"
    GESTURES_DRAGGING = "gestures_dragging"
    TARGET_SIZE = "target_size"


class WCAGLevel(str, Enum):
    """WCAG conformance level."""

    A = "A"
    AA = "AA"
    AAA = "AAA"


class VerificationDepth(str, Enum):
    """Depth of verification for a coverage matrix cell."""

    SIGNAL = "signal"  # Automated scan only
    MANUAL = "manual"  # Manual testing
    AT_VERIFIED = "at_verified"  # Assistive technology verified


# ---------------------------------------------------------------------------
# Evidence Models
# ---------------------------------------------------------------------------


class EvidenceArtifact(BaseModel):
    """A single evidence artifact reference."""

    artifact_type: str = Field(
        ..., description="Type: screenshot, video, dom_snapshot, a11y_tree, log, audio"
    )
    ref: str = Field(..., description="Storage reference (path or URI)")
    description: str = Field(default="", description="Brief description of the artifact")
    timestamp: Optional[datetime] = Field(default=None)


class EnvironmentInfo(BaseModel):
    """Environment information for evidence capture."""

    surface: Surface
    browser_or_device: str = Field(default="", description="e.g., Chrome 120, iPhone 15")
    os_version: str = Field(default="", description="e.g., macOS 14.2, iOS 17.2")
    viewport_or_scale: str = Field(default="", description="e.g., 1920x1080, 200% zoom, large text")
    assistive_tech: Optional[str] = Field(default=None, description="e.g., NVDA 2024.1, VoiceOver")


class EvidencePack(BaseModel):
    """Bundle of evidence artifacts for a finding."""

    pack_ref: str = Field(..., description="Unique reference for this evidence pack")
    finding_id: str = Field(..., description="ID of the associated finding")
    artifacts: List[EvidenceArtifact] = Field(default_factory=list)
    environment: EnvironmentInfo
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    notes: str = Field(default="", description="Additional context or notes")


# ---------------------------------------------------------------------------
# Finding Model (Core)
# ---------------------------------------------------------------------------


class WCAGMapping(BaseModel):
    """WCAG success criteria mapping with confidence."""

    sc: str = Field(..., description="Success criterion, e.g., 2.4.7")
    name: str = Field(default="", description="SC name, e.g., Focus Visible")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in this mapping")
    rationale: str = Field(default="", description="Why this SC applies")


class Finding(BaseModel):
    """
    A single accessibility finding.

    A finding is "reportable" only if it includes:
    - Repro steps that a dev can follow
    - Expected vs actual
    - User impact statement
    - Evidence artifacts (at least one)
    - Standards mapping (WCAG SC + confidence)
    - Remediation notes + acceptance criteria + test plan
    """

    id: str = Field(..., description="Unique finding ID")
    state: FindingState = Field(default=FindingState.DRAFT)
    surface: Surface
    target: str = Field(..., description="URL/screen/component/state being tested")
    issue_type: IssueType
    severity: Severity
    scope: Scope
    confidence: float = Field(..., ge=0.0, le=1.0, description="Overall confidence in this finding")

    # Core finding details
    title: str = Field(..., description="Short, descriptive title")
    summary: str = Field(..., description="Brief summary of the issue")
    repro_steps: List[str] = Field(default_factory=list, description="Steps to reproduce")
    expected: str = Field(..., description="Expected accessible behavior")
    actual: str = Field(..., description="Actual observed behavior")
    user_impact: str = Field(..., description="Who is harmed and how (user impact statement)")

    # Standards mapping
    wcag_mappings: List[WCAGMapping] = Field(
        default_factory=list, description="WCAG SC mappings with confidence"
    )
    section_508_tags: List[str] = Field(
        default_factory=list, description="Section 508 reporting tags"
    )

    # Evidence
    evidence_pack_ref: Optional[str] = Field(default=None, description="Reference to evidence pack")

    # Remediation
    root_cause_hypothesis: str = Field(default="", description="Hypothesis about the root cause")
    recommended_fix: List[str] = Field(default_factory=list, description="Fix recipe steps")
    acceptance_criteria: List[str] = Field(
        default_factory=list, description="Testable acceptance criteria"
    )
    test_plan: List[str] = Field(default_factory=list, description="Verification test steps")
    code_examples_ref: Optional[str] = Field(default=None, description="Reference to code examples")

    # Linking for pattern clustering
    pattern_id: Optional[str] = Field(
        default=None, description="Pattern cluster ID (assigned by QCR)"
    )
    component_id: Optional[str] = Field(default=None, description="Design system component ID")
    duplicate_of: Optional[str] = Field(default=None, description="ID of finding this duplicates")

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = Field(default="", description="Agent that created this finding")
    verified_by: Optional[str] = Field(default=None, description="Agent that verified this finding")


# ---------------------------------------------------------------------------
# Audit Plan Models
# ---------------------------------------------------------------------------


class MobileAppTarget(BaseModel):
    """Mobile app target specification."""

    platform: Literal["ios", "android"]
    name: str
    version: str
    build: str = Field(default="")


class AuditTargets(BaseModel):
    """Targets to be audited."""

    web_urls: List[str] = Field(default_factory=list)
    mobile_apps: List[MobileAppTarget] = Field(default_factory=list)


class AuditConstraints(BaseModel):
    """Constraints for the audit."""

    timebox_hours: Optional[int] = Field(default=None, description="Maximum hours for the audit")
    environments: List[str] = Field(
        default_factory=lambda: ["prod"], description="Environments to test"
    )
    auth_required: bool = Field(default=False, description="Whether authentication is required")


class SamplingStrategy(BaseModel):
    """Sampling strategy for the audit."""

    max_pages: Optional[int] = Field(default=None, description="Maximum pages/screens to test")
    strategy: Literal["journey_based", "template_based", "risk_based"] = Field(
        default="journey_based"
    )


class TestRunConfig(BaseModel):
    """Configuration for test runs."""

    browsers: List[str] = Field(
        default_factory=lambda: ["chromium"], description="Browsers to test"
    )
    viewports: List[Dict[str, int]] = Field(
        default_factory=lambda: [{"width": 1920, "height": 1080}]
    )
    mobile_devices: List[str] = Field(default_factory=list, description="Mobile devices to test")
    assistive_technologies: List[str] = Field(default_factory=lambda: ["nvda", "voiceover"])
    wcag_version: str = Field(default="2.2")
    wcag_levels: List[WCAGLevel] = Field(default_factory=lambda: [WCAGLevel.A, WCAGLevel.AA])


class AuditPlan(BaseModel):
    """
    Complete audit plan created during intake phase.

    Defines scope, targets, constraints, and sampling strategy.
    """

    audit_id: str = Field(..., description="Unique audit identifier")
    name: str = Field(default="", description="Human-readable audit name")
    targets: AuditTargets = Field(default_factory=AuditTargets)
    constraints: AuditConstraints = Field(default_factory=AuditConstraints)
    critical_journeys: List[str] = Field(
        default_factory=list, description="Critical user journeys to prioritize"
    )
    sampling: SamplingStrategy = Field(default_factory=SamplingStrategy)
    test_run_config: TestRunConfig = Field(default_factory=TestRunConfig)

    # References to related artifacts
    coverage_matrix_ref: Optional[str] = Field(default=None)
    inventory_ref: Optional[str] = Field(default=None)

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = Field(default="APL", description="Agent that created this plan")


# ---------------------------------------------------------------------------
# Coverage Matrix
# ---------------------------------------------------------------------------


class CoverageRow(BaseModel):
    """Single row in the coverage matrix."""

    sc: str = Field(..., description="WCAG Success Criterion, e.g., 2.4.7")
    sc_name: str = Field(default="", description="SC name")
    surfaces: List[Surface] = Field(default_factory=list)
    journeys: List[str] = Field(default_factory=list)
    depth: VerificationDepth = Field(default=VerificationDepth.SIGNAL)
    status: Literal["not_started", "in_progress", "complete"] = Field(default="not_started")
    findings_count: int = Field(default=0)


class CoverageMatrix(BaseModel):
    """
    SC x Surface x Journey coverage matrix.

    Tracks what has been tested and to what depth.
    """

    matrix_ref: str = Field(..., description="Unique reference for this matrix")
    audit_id: str
    wcag_version: str = Field(default="2.2")
    rows: List[CoverageRow] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Pattern Clustering
# ---------------------------------------------------------------------------


class PatternCluster(BaseModel):
    """
    A cluster of related findings representing a systemic pattern.

    Used by QCR to deduplicate and group findings.
    """

    pattern_id: str = Field(..., description="Unique pattern ID")
    name: str = Field(..., description="Pattern name")
    description: str = Field(default="")
    linked_finding_ids: List[str] = Field(default_factory=list)
    severity: Severity
    scope: Scope
    issue_types: List[IssueType] = Field(default_factory=list)
    wcag_scs: List[str] = Field(default_factory=list)
    component_ids: List[str] = Field(default_factory=list)
    fix_priority: int = Field(
        default=0, description="Priority for fixing (lower = higher priority)"
    )


# ---------------------------------------------------------------------------
# Phase Results
# ---------------------------------------------------------------------------


class IntakeResult(BaseModel):
    """Output of the Intake phase (Phase 0)."""

    success: bool = Field(default=False)
    audit_plan: Optional[AuditPlan] = None
    coverage_matrix: Optional[CoverageMatrix] = None
    test_run_config: Optional[TestRunConfig] = None
    summary: str = Field(default="")
    error: Optional[str] = None


class ScanResult(BaseModel):
    """Result from an automated scanner."""

    tool: str = Field(..., description="Scanner tool: axe, lighthouse, pa11y")
    url: str
    violations: List[Dict[str, Any]] = Field(default_factory=list)
    passes: int = Field(default=0)
    incomplete: int = Field(default=0)
    raw_ref: Optional[str] = Field(default=None, description="Reference to raw results")


class DiscoveryResult(BaseModel):
    """Output of the Discovery phase (Phase 1)."""

    success: bool = Field(default=False)
    draft_findings: List[Finding] = Field(default_factory=list)
    scan_results: List[ScanResult] = Field(default_factory=list)
    initial_patterns: List[PatternCluster] = Field(default_factory=list)
    pages_scanned: int = Field(default=0)
    summary: str = Field(default="")
    error: Optional[str] = None


class VerificationResult(BaseModel):
    """Output of the Verification phase (Phase 2)."""

    success: bool = Field(default=False)
    verified_findings: List[Finding] = Field(default_factory=list)
    rejected_findings: List[str] = Field(
        default_factory=list, description="IDs of findings rejected during verification"
    )
    summary: str = Field(default="")
    error: Optional[str] = None


class ReportPackagingResult(BaseModel):
    """Output of the Report Packaging phase (Phase 3)."""

    success: bool = Field(default=False)
    final_backlog: List[Finding] = Field(default_factory=list)
    patterns: List[PatternCluster] = Field(default_factory=list)
    executive_summary: str = Field(default="")
    roadmap: List[str] = Field(default_factory=list)
    coverage_matrix: Optional[CoverageMatrix] = None
    export_refs: Dict[str, str] = Field(
        default_factory=dict, description="References to exported artifacts"
    )
    summary: str = Field(default="")
    error: Optional[str] = None


class RetestResult(BaseModel):
    """Output of the Retest phase (Phase 4)."""

    success: bool = Field(default=False)
    findings_retested: int = Field(default=0)
    findings_closed: int = Field(default=0)
    findings_still_open: int = Field(default=0)
    updated_findings: List[Finding] = Field(default_factory=list)
    summary: str = Field(default="")
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Workflow Result
# ---------------------------------------------------------------------------


class AccessibilityAuditResult(BaseModel):
    """
    Complete result of the accessibility audit workflow.

    Captures outcome of all phases:
    Intake → Discovery → Verification → Report Packaging → (optional) Retest
    """

    audit_id: str
    success: bool = Field(default=False)
    current_phase: Phase = Field(default=Phase.INTAKE)
    completed_phases: List[Phase] = Field(default_factory=list)

    # Phase results
    intake_result: Optional[IntakeResult] = None
    discovery_result: Optional[DiscoveryResult] = None
    verification_result: Optional[VerificationResult] = None
    report_packaging_result: Optional[ReportPackagingResult] = None
    retest_result: Optional[RetestResult] = None

    # Final outputs
    final_findings: List[Finding] = Field(default_factory=list)
    final_patterns: List[PatternCluster] = Field(default_factory=list)
    coverage_matrix: Optional[CoverageMatrix] = None

    # Counts
    total_findings: int = Field(default=0)
    critical_count: int = Field(default=0)
    high_count: int = Field(default=0)
    medium_count: int = Field(default=0)
    low_count: int = Field(default=0)

    # Metadata
    summary: str = Field(default="")
    failure_reason: str = Field(default="")
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Typed Phase Contexts
# ---------------------------------------------------------------------------


class IntakeContext(BaseModel):
    """Typed context for the Intake phase."""

    phase: Phase = Phase.INTAKE
    audit_request: Optional["AuditRequest"] = None


class DiscoveryContext(BaseModel):
    """Typed context for the Discovery phase."""

    phase: Phase = Phase.DISCOVERY
    audit_id: str = ""
    urls: List[str] = Field(default_factory=list)
    apps: List[Dict[str, Any]] = Field(default_factory=list)


class VerificationContext(BaseModel):
    """Typed context for the Verification phase."""

    phase: Phase = Phase.VERIFICATION
    audit_id: str = ""
    findings: List[Finding] = Field(default_factory=list)
    stack: Dict[str, str] = Field(default_factory=dict)


class ReportPackagingContext(BaseModel):
    """Typed context for the Report Packaging phase."""

    phase: Phase = Phase.REPORT_PACKAGING
    audit_id: str = ""
    findings: List[Finding] = Field(default_factory=list)
    patterns: List[PatternCluster] = Field(default_factory=list)


class RetestContext(BaseModel):
    """Typed context for the Retest phase."""

    phase: Phase = Phase.RETEST
    audit_id: str = ""
    findings: List[Finding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API Request/Response Models
# ---------------------------------------------------------------------------


class AuditRequest(BaseModel):
    """Request to create and run an accessibility audit."""

    audit_id: Optional[str] = Field(default=None, description="Optional custom audit ID")
    name: str = Field(default="", description="Human-readable audit name")
    web_urls: List[str] = Field(default_factory=list)
    mobile_apps: List[MobileAppTarget] = Field(default_factory=list)
    critical_journeys: List[str] = Field(default_factory=list)
    timebox_hours: Optional[int] = None
    environments: List[str] = Field(default_factory=lambda: ["prod"])
    auth_required: bool = Field(default=False)
    max_pages: Optional[int] = None
    sampling_strategy: Literal["journey_based", "template_based", "risk_based"] = Field(
        default="journey_based"
    )
    wcag_levels: List[WCAGLevel] = Field(default_factory=lambda: [WCAGLevel.A, WCAGLevel.AA])


class AuditJobResponse(BaseModel):
    """Response when starting an audit job."""

    job_id: str
    audit_id: str
    status: str
    message: str


class AuditStatusResponse(BaseModel):
    """Response for audit status queries."""

    job_id: str
    audit_id: str
    status: str
    current_phase: Optional[str] = None
    progress: int = Field(default=0, description="Progress percentage 0-100")
    completed_phases: List[str] = Field(default_factory=list)
    findings_count: int = Field(default=0)
    error: Optional[str] = None
    result: Optional[AccessibilityAuditResult] = None


class FindingsListResponse(BaseModel):
    """Response for findings list queries."""

    audit_id: str
    total: int
    findings: List[Finding] = Field(default_factory=list)
    by_severity: Dict[str, int] = Field(default_factory=dict)
    by_issue_type: Dict[str, int] = Field(default_factory=dict)
    offset: int = Field(default=0)
    limit: int = Field(default=50)
    has_more: bool = Field(default=False)


class BacklogExportResponse(BaseModel):
    """Response for backlog export."""

    audit_id: str
    format: str
    artifact_ref: str
    counts: Dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Monitoring Models (ARM Add-on)
# ---------------------------------------------------------------------------


class MonitoringBaseline(BaseModel):
    """Baseline for accessibility regression monitoring."""

    baseline_ref: str
    audit_id: str
    env: Literal["stage", "prod"]
    targets: List[Dict[str, str]] = Field(default_factory=list)
    checks: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    snapshot_refs: List[str] = Field(default_factory=list)


class MonitoringRunResult(BaseModel):
    """Result of a monitoring check run."""

    run_id: str
    baseline_ref: str
    env: str
    results_ref: str
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MonitoringDiff(BaseModel):
    """Diff between monitoring run and baseline."""

    run_id: str
    baseline_ref: str
    new_issues: List[Dict[str, Any]] = Field(default_factory=list)
    resolved_issues: List[Dict[str, Any]] = Field(default_factory=list)
    unchanged_issues: List[Dict[str, Any]] = Field(default_factory=list)
    alerts_triggered: int = Field(default=0)


# ---------------------------------------------------------------------------
# Design System Models (ADSE Add-on)
# ---------------------------------------------------------------------------


class ComponentInventory(BaseModel):
    """Inventory of design system components."""

    inventory_ref: str
    system_name: str
    source: Literal["storybook", "repo", "manual"]
    components: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class A11yContract(BaseModel):
    """Accessibility contract for a design system component."""

    contract_ref: str
    system_name: str
    component: str
    platform: Surface
    requirements: Dict[str, Any] = Field(default_factory=dict)
    test_harness_plan: Dict[str, Any] = Field(default_factory=dict)
    linked_patterns: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Training Models (AET Add-on)
# ---------------------------------------------------------------------------


class TrainingModule(BaseModel):
    """Training module generated from audit patterns."""

    module_id: str
    title: str
    path_ref: str
    linked_patterns: List[str] = Field(default_factory=list)
    target_roles: List[str] = Field(default_factory=list)
    stacks: Dict[str, str] = Field(default_factory=dict)


class TrainingBundle(BaseModel):
    """Bundle of training modules."""

    bundle_id: str
    audit_id: str
    modules: List[TrainingModule] = Field(default_factory=list)
    publish_ref: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
