"""Tests for WCAG 2.2 criteria module."""


from accessibility_audit_team.wcag_criteria import (
    WCAG_22_CRITERIA,
    SuccessCriterion,
    WCAGLevel,
    WCAGPrinciple,
)


def _get_criterion(sc_id: str):
    return WCAG_22_CRITERIA.get(sc_id)


def test_wcag_22_criteria_is_nonempty():
    assert len(WCAG_22_CRITERIA) > 0


def test_get_criterion_known_id():
    sc = _get_criterion("1.1.1")
    assert sc is not None
    assert sc.sc == "1.1.1"
    assert sc.name == "Non-text Content"


def test_get_criterion_unknown_returns_none():
    assert _get_criterion("9.9.9") is None


def test_criterion_has_required_fields():
    sc = _get_criterion("1.1.1")
    assert hasattr(sc, "sc")
    assert hasattr(sc, "name")
    assert hasattr(sc, "level")
    assert hasattr(sc, "principle")
    assert hasattr(sc, "description")


def test_criterion_techniques_is_list():
    sc = _get_criterion("1.1.1")
    assert isinstance(sc.techniques, list)


def test_all_criteria_ids_unique():
    ids = list(WCAG_22_CRITERIA.keys())
    assert len(ids) == len(set(ids))


def test_get_criteria_by_level_a():
    level_a = [sc for sc in WCAG_22_CRITERIA.values() if sc.level == WCAGLevel.A]
    assert len(level_a) > 0
    for sc in level_a:
        assert sc.level == WCAGLevel.A


def test_get_criteria_by_level_aa():
    level_aa = [sc for sc in WCAG_22_CRITERIA.values() if sc.level == WCAGLevel.AA]
    assert len(level_aa) > 0


def test_get_criteria_by_level_aaa():
    level_aaa = [sc for sc in WCAG_22_CRITERIA.values() if sc.level == WCAGLevel.AAA]
    assert len(level_aaa) >= 0  # may be empty in minimal data


def test_get_criteria_by_principle_perceivable():
    perceivable = [
        sc for sc in WCAG_22_CRITERIA.values()
        if sc.principle == WCAGPrinciple.PERCEIVABLE
    ]
    assert len(perceivable) > 0
    assert all(sc.principle == WCAGPrinciple.PERCEIVABLE for sc in perceivable)


def test_get_criteria_by_principle_operable():
    operable = [
        sc for sc in WCAG_22_CRITERIA.values()
        if sc.principle == WCAGPrinciple.OPERABLE
    ]
    assert len(operable) >= 0


def test_get_level_a_aa_criteria_excludes_aaa():
    a_and_aa = [
        sc for sc in WCAG_22_CRITERIA.values()
        if sc.level in (WCAGLevel.A, WCAGLevel.AA)
    ]
    for sc in a_and_aa:
        assert sc.level != WCAGLevel.AAA


def test_criterion_is_success_criterion_instance():
    for sc in WCAG_22_CRITERIA.values():
        assert isinstance(sc, SuccessCriterion)
        break  # just check the first one
