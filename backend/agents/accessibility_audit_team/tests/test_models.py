"""Tests for accessibility_audit_team models."""


from accessibility_audit_team.models import (
    AuditPlan,
    AuditRequest,
    AuditTargets,
    CoverageMatrix,
    CoverageRow,
    EnvironmentInfo,
    EvidencePack,
    Finding,
    FindingState,
    IssueType,
    Phase,
    Scope,
    Severity,
    Surface,
    VerificationDepth,
    WCAGLevel,
    WCAGMapping,
)


def test_phase_enum_values():
    assert Phase.INTAKE == "intake"
    assert Phase.DISCOVERY == "discovery"
    assert Phase.VERIFICATION == "verification"
    assert Phase.REPORT_PACKAGING == "report_packaging"
    assert Phase.RETEST == "retest"


def test_severity_enum_values():
    assert Severity.CRITICAL == "Critical"
    assert Severity.HIGH == "High"
    assert Severity.MEDIUM == "Medium"
    assert Severity.LOW == "Low"


def test_surface_enum_values():
    assert Surface.WEB == "web"
    assert Surface.IOS == "ios"
    assert Surface.ANDROID == "android"


def test_finding_instantiation():
    f = Finding(
        id="F-001",
        surface=Surface.WEB,
        target="https://example.com/",
        issue_type=IssueType.CONTRAST,
        severity=Severity.HIGH,
        scope=Scope.LOCALIZED,
        confidence=0.9,
        title="Low contrast text",
        summary="Text contrast ratio is 2.5:1",
        expected="Contrast ratio of at least 4.5:1",
        actual="Contrast ratio of 2.5:1",
        user_impact="Visually impaired users cannot read text",
    )
    assert f.id == "F-001"
    assert f.severity == Severity.HIGH
    assert f.state == FindingState.DRAFT
    assert f.wcag_mappings == []


def test_audit_plan_defaults():
    plan = AuditPlan(audit_id="audit-1")
    assert plan.audit_id == "audit-1"
    assert plan.name == ""
    assert plan.critical_journeys == []
    assert plan.created_by == "APL"


def test_audit_plan_with_targets():
    plan = AuditPlan(
        audit_id="audit-2",
        name="Home page audit",
        targets=AuditTargets(web_urls=["https://example.com"]),
    )
    assert plan.targets.web_urls == ["https://example.com"]


def test_evidence_pack_instantiation():
    pack = EvidencePack(
        pack_ref="EP-001",
        finding_id="F-001",
        environment=EnvironmentInfo(surface=Surface.WEB),
    )
    assert pack.pack_ref == "EP-001"
    assert pack.finding_id == "F-001"
    assert pack.artifacts == []


def test_audit_request_defaults():
    req = AuditRequest()
    assert req.web_urls == []
    assert req.auth_required is False
    assert req.sampling_strategy == "journey_based"
    assert WCAGLevel.A in req.wcag_levels


def test_audit_request_with_urls():
    req = AuditRequest(
        name="Test audit",
        web_urls=["https://example.com", "https://example.com/about"],
        wcag_levels=[WCAGLevel.A, WCAGLevel.AA],
    )
    assert len(req.web_urls) == 2
    assert WCAGLevel.AA in req.wcag_levels


def test_coverage_matrix_row():
    row = CoverageRow(sc="1.1.1")
    assert row.sc == "1.1.1"
    assert row.depth == VerificationDepth.SIGNAL
    assert row.status == "not_started"
    assert row.findings_count == 0


def test_coverage_matrix():
    matrix = CoverageMatrix(matrix_ref="CM-1", audit_id="audit-1")
    assert matrix.matrix_ref == "CM-1"
    assert matrix.rows == []
    assert matrix.wcag_version == "2.2"


def test_wcag_mapping():
    m = WCAGMapping(sc="1.1.1", confidence=0.95)
    assert m.sc == "1.1.1"
    assert m.confidence == 0.95
    assert m.name == ""
