"""Tests for WCAG 2.2 criteria module."""

from accessibility_audit_team.wcag_criteria import (
    WCAG_22_CRITERIA,
    SuccessCriterion,
    WCAGLevel,
    WCAGPrinciple,
    get_criteria_by_level,
    get_criteria_by_principle,
    get_criterion,
    get_level_a_aa_criteria,
)

# The complete WCAG 2.2 Level A and AA sets. 4.1.1 Parsing is absent from Level A
# because WCAG 2.2 removed it.
LEVEL_A_WCAG_22 = {
    "1.1.1",
    "1.2.1",
    "1.2.2",
    "1.2.3",
    "1.3.1",
    "1.3.2",
    "1.3.3",
    "1.4.1",
    "1.4.2",
    "2.1.1",
    "2.1.2",
    "2.1.4",
    "2.2.1",
    "2.2.2",
    "2.3.1",
    "2.4.1",
    "2.4.2",
    "2.4.3",
    "2.4.4",
    "2.5.1",
    "2.5.2",
    "2.5.3",
    "2.5.4",
    "3.1.1",
    "3.2.1",
    "3.2.2",
    "3.2.6",
    "3.3.1",
    "3.3.2",
    "3.3.7",
    "4.1.2",
}
# WCAG 2.2 has 31 Level AAA criteria; this table deliberately carries only these.
LEVEL_AAA_PRESENT = {"2.4.12", "2.4.13", "3.3.9"}
LEVEL_AA_WCAG_22 = {
    "1.2.4",
    "1.2.5",
    "1.3.4",
    "1.3.5",
    "1.4.3",
    "1.4.4",
    "1.4.5",
    "1.4.10",
    "1.4.11",
    "1.4.12",
    "1.4.13",
    "2.4.5",
    "2.4.6",
    "2.4.7",
    "2.4.11",
    "2.5.7",
    "2.5.8",
    "3.1.2",
    "3.2.3",
    "3.2.4",
    "3.3.3",
    "3.3.4",
    "3.3.8",
    "4.1.3",
}


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


def test_get_criteria_by_principle_perceivable():
    perceivable = [
        sc for sc in WCAG_22_CRITERIA.values() if sc.principle == WCAGPrinciple.PERCEIVABLE
    ]
    assert len(perceivable) > 0
    assert all(sc.principle == WCAGPrinciple.PERCEIVABLE for sc in perceivable)


def test_get_criteria_by_principle_operable():
    """Every Principle 2 criterion is reachable through the shipped accessor.

    Preconditions:
        None.

    Postconditions:
        Asserts the accessor agrees with the table; does not mutate the table.
    """
    expected = {sc.sc for sc in WCAG_22_CRITERIA.values() if sc.principle == WCAGPrinciple.OPERABLE}
    assert {sc.sc for sc in get_criteria_by_principle(WCAGPrinciple.OPERABLE)} == expected
    assert expected, "Principle 2 must not be empty"


def test_get_level_a_aa_criteria_excludes_aaa():
    """The shipped accessor returns exactly Level A plus Level AA.

    Filtering the table inline and asserting the result excludes AAA is true by
    construction and cannot catch a regression in ``get_level_a_aa_criteria`` — the
    accessor ``build_coverage_matrix`` depends on. Compare against the pinned sets
    instead.

    Preconditions:
        None.

    Postconditions:
        Asserts the accessor's membership; does not mutate the table.
    """
    returned = {sc.sc for sc in get_level_a_aa_criteria()}
    assert returned == LEVEL_A_WCAG_22 | LEVEL_AA_WCAG_22
    assert all(sc.level != WCAGLevel.AAA for sc in get_level_a_aa_criteria())


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


def test_level_a_and_aa_sets_are_complete():
    """Pin the A and AA sets by MEMBERSHIP, not cardinality.

    The module docstring states these are the complete WCAG 2.2 sets. A count alone
    would miss a swap — mis-numbering one key, or mis-levelling one criterion in each
    direction, keeps the totals right while the set is wrong. Comparing sets makes
    pytest print the symmetric difference, naming the criterion that drifted.

    Preconditions:
        None.

    Postconditions:
        Asserts both sets exactly; does not mutate the table.
    """
    assert {sc.sc for sc in get_criteria_by_level(WCAGLevel.A)} == LEVEL_A_WCAG_22
    assert {sc.sc for sc in get_criteria_by_level(WCAGLevel.AA)} == LEVEL_AA_WCAG_22


def test_level_aaa_is_deliberately_partial():
    """AAA coverage is 3 of WCAG 2.2's 31, by choice rather than omission-as-bug.

    Pinned so the module docstring's third coverage claim cannot drift unnoticed.

    Preconditions:
        None.

    Postconditions:
        Asserts the AAA set; does not mutate the table.
    """
    assert {sc.sc for sc in get_criteria_by_level(WCAGLevel.AAA)} == LEVEL_AAA_PRESENT


def test_keys_match_their_criterion_number():
    """Every dict key equals its entry's ``sc`` field — a module invariant.

    A copy-pasted entry keyed "1.4.14" whose body still says sc="1.4.13" passes the
    membership tests (they read ``sc.sc``, not the key) while ``get_criterion``
    returns a criterion that misreports itself to ``map_wcag``.

    Preconditions:
        None.

    Postconditions:
        Asserts the key/field agreement; does not mutate the table.
    """
    mismatched = {k: v.sc for k, v in WCAG_22_CRITERIA.items() if k != v.sc}
    assert mismatched == {}, f"keys disagreeing with their sc field: {mismatched}"


def test_f109_only_on_accessible_authentication():
    """F109 fails 3.3.8 and 3.3.9 only; it is not a target-size failure technique.

    Preconditions:
        None.

    Postconditions:
        Asserts the attachment set; does not mutate the table.
    """
    carriers = {sc.sc for sc in WCAG_22_CRITERIA.values() if "F109" in sc.failures}
    assert carriers == {"3.3.8", "3.3.9"}
