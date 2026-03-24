"""Tests for Section 508 criteria module."""

from accessibility_audit_team.section_508_criteria import (
    SECTION_508_REQUIREMENTS,
    ApplicablePlatform,
    Section508Category,
    get_508_requirements_for_wcag,
    get_508_tags_for_wcag_list,
    get_mobile_requirements,
    get_requirement,
    get_requirements_by_category,
    get_requirements_by_platform,
    get_web_requirements,
)


def test_section_508_requirements_is_nonempty():
    assert len(SECTION_508_REQUIREMENTS) > 0


def test_get_requirement_known_id():
    req = get_requirement("302.1")
    assert req is not None
    assert req.section == "302.1"
    assert req.name == "Without Vision"


def test_get_requirement_unknown_returns_none():
    assert get_requirement("999.99") is None


def test_requirement_has_required_fields():
    req = get_requirement("302.1")
    assert hasattr(req, "section")
    assert hasattr(req, "name")
    assert hasattr(req, "category")
    assert hasattr(req, "wcag_mappings")
    assert hasattr(req, "platforms")


def test_get_508_requirements_for_wcag_known_criterion():
    reqs = get_508_requirements_for_wcag("1.1.1")
    assert isinstance(reqs, list)
    # 1.1.1 should appear in at least one 508 requirement
    all_mappings = [m for r in SECTION_508_REQUIREMENTS.values() for m in r.wcag_mappings]
    if "1.1.1" in all_mappings:
        assert len(reqs) > 0


def test_get_508_requirements_for_wcag_unknown_returns_empty():
    reqs = get_508_requirements_for_wcag("9.9.9")
    assert reqs == []


def test_get_requirements_by_category():
    general_reqs = get_requirements_by_category(Section508Category.GENERAL)
    assert isinstance(general_reqs, list)
    for req in general_reqs:
        assert req.category == Section508Category.GENERAL


def test_get_requirements_by_platform():
    web_reqs = get_requirements_by_platform(ApplicablePlatform.WEB)
    assert isinstance(web_reqs, list)
    for req in web_reqs:
        assert ApplicablePlatform.WEB in req.platforms


def test_get_web_requirements():
    reqs = get_web_requirements()
    assert isinstance(reqs, list)
    assert len(reqs) > 0


def test_get_mobile_requirements():
    reqs = get_mobile_requirements()
    assert isinstance(reqs, list)


def test_get_508_tags_for_wcag_list():
    tags = get_508_tags_for_wcag_list(["1.1.1", "1.4.3"])
    assert isinstance(tags, list)


def test_get_508_tags_for_empty_list():
    tags = get_508_tags_for_wcag_list([])
    assert tags == []
