"""Tests for the architecture audit template, scoring tools, and agent."""

import yaml
from agents.accessibility_audit_team.a11y_agency_strands.app.agents.architecture_agent import (
    _extract_business_impact,
    run_architecture_audit,
)
from agents.accessibility_audit_team.a11y_agency_strands.app.agents.base import ToolContext
from agents.accessibility_audit_team.a11y_agency_strands.app.tools.architecture_tools import (
    build_architecture_audit_report,
    load_architecture_audit_template,
    pct_to_grade,
    score_architecture_section,
)

# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------


def test_template_loads_and_has_expected_structure():
    template = load_architecture_audit_template()
    assert "sections" in template
    assert "scoring" in template
    assert "methodology" in template
    assert len(template["sections"]) == 12


def test_template_yaml_is_valid():
    """Verify the YAML parses without error via a cold load."""
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent.parent
        / "assets"
        / "site_architecture_audit_template.yaml"
    )
    with open(path) as fh:
        data = yaml.safe_load(fh)
    assert data["template_version"] == "1.0"


def test_all_checklist_items_have_unique_ids():
    template = load_architecture_audit_template()
    ids = []
    for section in template["sections"]:
        for sub in section.get("subsections", []):
            for item in sub.get("checklist_items", []):
                ids.append(item["id"])
    assert len(ids) == len(set(ids)), f"Duplicate IDs found: {[x for x in ids if ids.count(x) > 1]}"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


def test_pct_to_grade_thresholds():
    assert pct_to_grade(100.0) == "Excellent"
    assert pct_to_grade(90.0) == "Excellent"
    assert pct_to_grade(89.9) == "Good"
    assert pct_to_grade(75.0) == "Good"
    assert pct_to_grade(74.9) == "Needs Improvement"
    assert pct_to_grade(50.0) == "Needs Improvement"
    assert pct_to_grade(49.9) == "Poor"
    assert pct_to_grade(0.0) == "Poor"


# ---------------------------------------------------------------------------
# Section scoring
# ---------------------------------------------------------------------------


def test_score_section_all_pass():
    results = [
        {"id": "a", "label": "Item A", "passed": True},
        {"id": "b", "label": "Item B", "passed": True},
    ]
    section = score_architecture_section("sec1", "Section 1", results)
    assert section.passed_count == 2
    assert section.tested_count == 2
    assert section.total_count == 2
    assert section.score_pct == 100.0
    assert section.grade == "Excellent"
    assert section.issues == []


def test_score_section_mixed():
    results = [
        {"id": "a", "label": "Item A", "passed": True},
        {"id": "b", "label": "Item B", "passed": False, "notes": "Broken"},
        {"id": "c", "label": "Item C", "passed": True},
    ]
    section = score_architecture_section("sec2", "Section 2", results)
    assert section.passed_count == 2
    assert section.tested_count == 3
    assert section.score_pct == round(2 / 3 * 100, 1)
    assert section.issues == ["Item B"]


def test_score_section_excludes_not_tested():
    """Items with passed=None should not count toward tested or score."""
    results = [
        {"id": "a", "label": "Item A", "passed": True},
        {"id": "b", "label": "Item B", "passed": None},  # not tested
        {"id": "c", "label": "Item C", "passed": False},
    ]
    section = score_architecture_section("sec3", "Section 3", results)
    assert section.total_count == 3  # total items in template
    assert section.tested_count == 2  # only a and c
    assert section.passed_count == 1
    assert section.score_pct == 50.0
    assert "Item B" not in section.issues  # not-tested items aren't failures
    assert "Item C" in section.issues


def test_score_section_all_not_tested():
    results = [
        {"id": "a", "label": "A", "passed": None},
        {"id": "b", "label": "B", "passed": None},
    ]
    section = score_architecture_section("sec4", "Section 4", results)
    assert section.tested_count == 0
    assert section.score_pct == 0.0


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def test_report_uses_equal_section_weighting():
    """Overall score should be mean of section percentages, not item-count-weighted."""
    # Section A: 1/1 = 100%
    sec_a = score_architecture_section("a", "A", [{"id": "a1", "label": "X", "passed": True}])
    # Section B: 1/3 = 33.3%
    sec_b = score_architecture_section(
        "b",
        "B",
        [
            {"id": "b1", "label": "X", "passed": True},
            {"id": "b2", "label": "Y", "passed": False},
            {"id": "b3", "label": "Z", "passed": False},
        ],
    )
    report = build_architecture_audit_report("https://example.com", [sec_a, sec_b])
    # Equal weighting: (100 + 33.3) / 2 = 66.65 → 66.7
    # Item-count-weighted would be 2/4 = 50.0
    expected = round((100.0 + round(1 / 3 * 100, 1)) / 2, 1)
    assert report.overall_score_pct == expected
    assert report.overall_score_pct != 50.0  # not item-count-weighted


def test_report_skips_untested_sections_in_overall():
    """Sections where nothing was tested should not pull down the overall score."""
    sec_tested = score_architecture_section("a", "A", [{"id": "a1", "label": "X", "passed": True}])
    sec_untested = score_architecture_section(
        "b", "B", [{"id": "b1", "label": "Y", "passed": None}]
    )
    report = build_architecture_audit_report("https://example.com", [sec_tested, sec_untested])
    assert report.overall_score_pct == 100.0  # only the tested section counts


def test_wcag_compliance_aggregation():
    results = [
        {"id": "a", "label": "A", "passed": True, "wcag_ref": "2.4.7", "wcag_level": "AA"},
        {"id": "b", "label": "B", "passed": False, "wcag_ref": "2.4.7"},
        {"id": "c", "label": "C", "passed": True, "wcag_ref": "2.1.1"},
    ]
    section = score_architecture_section("s", "S", results)
    report = build_architecture_audit_report("https://example.com", [section])

    sc_map = {s.sc: s for s in report.wcag_compliance}
    assert sc_map["2.4.7"].status == "partial"  # one pass, one fail
    assert sc_map["2.4.7"].name == "Focus Visible"
    assert sc_map["2.4.7"].wcag_level == "AA"
    assert sc_map["2.1.1"].status == "pass"


def test_wcag_not_tested_status():
    results = [
        {"id": "a", "label": "A", "passed": None, "wcag_ref": "2.4.1"},
    ]
    section = score_architecture_section("s", "S", results)
    report = build_architecture_audit_report("https://example.com", [section])
    sc_map = {s.sc: s for s in report.wcag_compliance}
    assert sc_map["2.4.1"].status == "not_tested"


# ---------------------------------------------------------------------------
# Business impact extraction
# ---------------------------------------------------------------------------


def test_extract_business_impact_from_overrides():
    overrides = {
        "bia_01": {"passed": True, "notes": "All tasks completable"},
        "bia_02": {"passed": False, "notes": "Screen reader blocked on checkout"},
        "bia_04": {"passed": True, "notes": "No legal risk identified"},
        "top_strengths": {"items": ["Semantic nav", "Skip links"]},
        "quick_wins": {"items": ["Add aria-current"]},
    }
    impact = _extract_business_impact(overrides)
    assert impact.keyboard_tasks_completable is True
    assert impact.screen_reader_tasks_completable is False
    assert impact.mobile_tasks_completable is None  # bia_03 not provided
    assert impact.legal_compliance_risk is True
    assert impact.top_strengths == ["Semantic nav", "Skip links"]
    assert impact.quick_wins == ["Add aria-current"]


# ---------------------------------------------------------------------------
# Full agent tool contract
# ---------------------------------------------------------------------------


def test_architecture_agent_tool_contract(tmp_path):
    overrides = {
        "ssm_01": {"passed": True, "notes": "5 templates found"},
        "nse_01": {"passed": True},
        "nse_03": {"passed": False, "notes": "Focus ring only 1px"},
    }
    context = ToolContext(
        {
            "artifact_root": str(tmp_path),
            "checklist_results": overrides,
            "recommendations": ["Fix focus indicators"],
        }
    )
    result = run_architecture_audit("https://example.com", context)
    assert result["phase"] == "architecture_audit"
    assert result["artifact"].endswith("architecture.json")
    assert result["overall_grade"] in {"Excellent", "Good", "Needs Improvement", "Poor"}
