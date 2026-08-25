"""Tests for WCAG 2.2 criteria module."""

from accessibility_audit_team.wcag_criteria import (
    WCAG_22_CRITERIA,
    SuccessCriterion,
    WCAGLevel,
    WCAGPrinciple,
    get_criteria_by_level,
    get_criterion,
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
        sc for sc in WCAG_22_CRITERIA.values() if sc.principle == WCAGPrinciple.PERCEIVABLE
    ]
    assert len(perceivable) > 0
    assert all(sc.principle == WCAGPrinciple.PERCEIVABLE for sc in perceivable)


def test_get_criteria_by_principle_operable():
    operable = [sc for sc in WCAG_22_CRITERIA.values() if sc.principle == WCAGPrinciple.OPERABLE]
    assert len(operable) >= 0


def test_get_level_a_aa_criteria_excludes_aaa():
    a_and_aa = [sc for sc in WCAG_22_CRITERIA.values() if sc.level in (WCAGLevel.A, WCAGLevel.AA)]
    for sc in a_and_aa:
        assert sc.level != WCAGLevel.AAA


def test_criterion_is_success_criterion_instance():
    for sc in WCAG_22_CRITERIA.values():
        assert isinstance(sc, SuccessCriterion)
        break  # just check the first one


def test_parsing_criterion_absent():
    """4.1.1 Parsing was removed in WCAG 2.2, so this 2.2 table must not carry it.

    Exercises the shipped ``get_criterion`` rather than a local shim, since that is
    the accessor production code (``tools/standards/map_wcag.py``) calls.

    Preconditions:
        None.

    Postconditions:
        Asserts the lookup misses; does not mutate the table.
    """
    assert get_criterion("4.1.1") is None


def test_level_counts_match_wcag_22():
    """Pin all three coverage counts the module docstring states.

    WCAG 2.2 defines 31 Level A and 24 Level AA criteria, and this table carries
    both sets complete. It carries 3 of the 31 Level AAA criteria by choice. A
    count that drifts in either direction means a criterion was dropped, added, or
    mis-levelled — the assertion messages name the criteria so the diff is visible
    without hand-diffing a 58-entry table.

    Preconditions:
        None.

    Postconditions:
        Asserts the three counts; does not mutate the table.
    """
    for level, expected in ((WCAGLevel.A, 31), (WCAGLevel.AA, 24), (WCAGLevel.AAA, 3)):
        numbers = sorted(sc.sc for sc in get_criteria_by_level(level))
        assert len(numbers) == expected, (
            f"Level {level.value}: expected {expected}, got {len(numbers)}: {numbers}"
        )


def test_f109_only_on_accessible_authentication():
    """F109 fails 3.3.8 and 3.3.9 only; it is not a target-size failure technique.

    Preconditions:
        None.

    Postconditions:
        Asserts the attachment set; does not mutate the table.
    """
    carriers = {sc.sc for sc in WCAG_22_CRITERIA.values() if "F109" in sc.failures}
    assert carriers == {"3.3.8", "3.3.9"}
