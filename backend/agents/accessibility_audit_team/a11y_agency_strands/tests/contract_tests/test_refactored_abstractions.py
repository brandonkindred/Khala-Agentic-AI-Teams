"""Tests for the refactored abstractions: AssetRegistry, GradingScale,
TemplateAuditEngine, PhaseResult, and flatten_checklist_items."""

from agents.accessibility_audit_team.a11y_agency_strands.app.models.grading import GradingScale
from agents.accessibility_audit_team.a11y_agency_strands.app.models.phase_result import (
    ArchitecturePhaseResult,
    ComponentAuditResult,
    PhaseResult,
)
from agents.accessibility_audit_team.a11y_agency_strands.app.tools.asset_registry import (
    AssetRegistry,
)
from agents.accessibility_audit_team.a11y_agency_strands.app.tools.template_audit_engine import (
    TemplateAuditEngine,
    _extract_sc_names_from_template,
    flatten_checklist_items,
)

# ---------------------------------------------------------------------------
# AssetRegistry
# ---------------------------------------------------------------------------


class TestAssetRegistry:
    def test_loads_yaml_by_name(self):
        data = AssetRegistry.load("site_architecture_audit_template.yaml")
        assert data["template_version"] == "1.0"

    def test_caches_across_calls(self):
        a = AssetRegistry.load("site_architecture_audit_template.yaml")
        b = AssetRegistry.load("site_architecture_audit_template.yaml")
        assert a is b

    def test_loads_case_study_templates(self):
        data = AssetRegistry.load("case_study_templates.yaml")
        assert "templates" in data


# ---------------------------------------------------------------------------
# GradingScale
# ---------------------------------------------------------------------------


class TestGradingScale:
    def test_from_template(self):
        template = AssetRegistry.load("site_architecture_audit_template.yaml")
        scale = GradingScale.from_template(template)
        assert scale.grade(100) == "Excellent"
        assert scale.grade(90) == "Excellent"
        assert scale.grade(89) == "Good"
        assert scale.grade(75) == "Good"
        assert scale.grade(50) == "Needs Improvement"
        assert scale.grade(49) == "Poor"

    def test_fallback_when_scoring_missing(self):
        scale = GradingScale.from_template({})
        assert scale.grade(95) == "Excellent"
        assert scale.grade(40) == "Poor"

    def test_frozen(self):
        scale = GradingScale.from_template({})
        try:
            scale.thresholds = ()  # type: ignore[misc]
            raise AssertionError("Should be frozen")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# flatten_checklist_items
# ---------------------------------------------------------------------------


class TestFlattenChecklistItems:
    def test_flattens_subsections(self):
        section = {
            "subsections": [
                {"checklist_items": [{"id": "a"}, {"id": "b"}]},
                {"checklist_items": [{"id": "c"}]},
            ]
        }
        assert [it["id"] for it in flatten_checklist_items(section)] == ["a", "b", "c"]

    def test_empty_section(self):
        assert flatten_checklist_items({}) == []
        assert flatten_checklist_items({"subsections": []}) == []


# ---------------------------------------------------------------------------
# SC names extraction from YAML (#6)
# ---------------------------------------------------------------------------


class TestSCNamesFromYAML:
    def test_extracts_names_from_wcag_compliance_summary(self):
        template = AssetRegistry.load("site_architecture_audit_template.yaml")
        names = _extract_sc_names_from_template(template)
        assert names["2.4.7"] == "Focus Visible"
        assert names["2.1.1"] == "Keyboard"
        assert names["4.1.3"] == "Status Messages"
        # Should have entries for both Level A and AA
        assert len(names) >= 15

    def test_returns_empty_for_missing_section(self):
        assert _extract_sc_names_from_template({"sections": []}) == {}


# ---------------------------------------------------------------------------
# TemplateAuditEngine
# ---------------------------------------------------------------------------


class TestTemplateAuditEngine:
    def test_evaluate_returns_result(self):
        engine = TemplateAuditEngine("site_architecture_audit_template.yaml")
        report = engine.evaluate("https://example.com")
        assert report.target == "https://example.com"
        assert len(report.sections) == 12
        assert report.overall_grade in {"Excellent", "Good", "Needs Improvement", "Poor"}

    def test_evaluate_with_overrides(self):
        engine = TemplateAuditEngine("site_architecture_audit_template.yaml")
        overrides = {"nse_01": {"passed": True}, "nse_02": {"passed": False}}
        report = engine.evaluate("https://example.com", overrides)
        # At least nse_01 and nse_02 should be in a section's items
        nav_section = next(
            s for s in report.sections if s.section_id == "navigation_system_evaluation"
        )
        item_map = {it.id: it for it in nav_section.items}
        assert item_map["nse_01"].passed is True
        assert item_map["nse_02"].passed is False

    def test_persist(self, tmp_path):
        engine = TemplateAuditEngine("site_architecture_audit_template.yaml")
        report = engine.evaluate("https://example.com")
        path = engine.persist(report, str(tmp_path))
        assert path.endswith("architecture.json")

    def test_grading_scale_is_from_template(self):
        engine = TemplateAuditEngine("site_architecture_audit_template.yaml")
        assert engine.grading.grade(95) == "Excellent"

    def test_wcag_names_derived_from_yaml(self):
        engine = TemplateAuditEngine("site_architecture_audit_template.yaml")
        assert "2.4.7" in engine._sc_names
        assert engine._sc_names["2.4.7"] == "Focus Visible"


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------


class TestPhaseResult:
    def test_base_model_dump(self):
        r = PhaseResult(phase="test", artifact="/path/to/artifact.json")
        d = r.model_dump()
        assert d["phase"] == "test"
        assert d["artifact"] == "/path/to/artifact.json"

    def test_subclass_includes_extra_fields(self):
        r = ComponentAuditResult(artifact="/a.json", finding_id="cmp-nav")
        d = r.model_dump()
        assert d["phase"] == "component_audit"
        assert d["finding_id"] == "cmp-nav"

    def test_architecture_result(self):
        r = ArchitecturePhaseResult(artifact="/a.json", overall_grade="Good")
        d = r.model_dump()
        assert d["phase"] == "architecture_audit"
        assert d["overall_grade"] == "Good"
