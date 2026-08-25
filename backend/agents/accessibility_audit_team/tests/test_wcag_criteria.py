"""Tests for WCAG 2.2 criteria module."""

import pytest

from accessibility_audit_team.wcag_criteria import (
    WCAG_22_CRITERIA,
    SuccessCriterion,
    WCAGLevel,
    WCAGPrinciple,
    get_all_sc_numbers,
    get_criteria_by_level,
    get_criteria_by_principle,
    get_criterion,
    get_guideline_criteria,
    get_level_a_aa_criteria,
    get_new_in_21_criteria,
    get_new_in_22_criteria,
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

# WCAG 2.2 has 31 Level AAA criteria; this table deliberately carries only these.
LEVEL_AAA_PRESENT = {"2.4.12", "2.4.13", "3.3.9"}


def test_wcag_22_criteria_is_nonempty():
    assert len(WCAG_22_CRITERIA) > 0


def test_get_criterion_known_id():
    """A known criterion number resolves to its stored entry.

    Preconditions:
        None.

    Postconditions:
        Asserts the returned entry's identity; does not mutate the table.
    """
    sc = get_criterion("1.1.1")
    assert sc is not None
    assert sc.sc == "1.1.1"
    assert sc.name == "Non-text Content"


def test_get_criterion_unknown_returns_none():
    """An unknown criterion number is not an error; it returns None.

    Preconditions:
        None.

    Postconditions:
        Asserts the None return; does not mutate the table.
    """
    assert get_criterion("9.9.9") is None


def test_criterion_has_required_fields():
    """A known criterion exposes the fields consumers read.

    Preconditions:
        None.

    Postconditions:
        Asserts the field set; does not mutate the table.
    """
    sc = get_criterion("1.1.1")
    assert sc is not None, "1.1.1 is missing from the table"
    for field in ("sc", "name", "level", "principle", "description"):
        assert hasattr(sc, field), f"1.1.1 is missing the {field} field"


def test_criterion_techniques_is_list():
    """``techniques`` is a list, so callers can iterate it without a type check.

    Preconditions:
        None.

    Postconditions:
        Asserts the field type; does not mutate the table.
    """
    sc = get_criterion("1.1.1")
    assert sc is not None, "1.1.1 is missing from the table"
    assert isinstance(sc.techniques, list)


def test_get_criteria_by_principle_matches_the_specification():
    """Every criterion is reachable through its principle, pinned by membership.

    Filtering the table inline and asserting the accessor against that same filter is
    true by construction and cannot catch a regression — the tautology the level
    tests were rewritten to avoid. Pin explicit sets instead, covering all four
    principles; the previous version of this test only checked two.

    Preconditions:
        None.

    Postconditions:
        Asserts each accessor's membership; does not mutate the table.
    """
    expected_by_principle = {
        WCAGPrinciple.PERCEIVABLE: {
            "1.1.1",
            "1.2.1",
            "1.2.2",
            "1.2.3",
            "1.2.4",
            "1.2.5",
            "1.3.1",
            "1.3.2",
            "1.3.3",
            "1.3.4",
            "1.3.5",
            "1.4.1",
            "1.4.2",
            "1.4.3",
            "1.4.4",
            "1.4.5",
            "1.4.10",
            "1.4.11",
            "1.4.12",
            "1.4.13",
        },
        WCAGPrinciple.OPERABLE: {
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
            "2.4.5",
            "2.4.6",
            "2.4.7",
            "2.4.11",
            "2.4.12",
            "2.4.13",
            "2.5.1",
            "2.5.2",
            "2.5.3",
            "2.5.4",
            "2.5.7",
            "2.5.8",
        },
        WCAGPrinciple.UNDERSTANDABLE: {
            "3.1.1",
            "3.1.2",
            "3.2.1",
            "3.2.2",
            "3.2.3",
            "3.2.4",
            "3.2.6",
            "3.3.1",
            "3.3.2",
            "3.3.3",
            "3.3.4",
            "3.3.7",
            "3.3.8",
            "3.3.9",
        },
        WCAGPrinciple.ROBUST: {"4.1.2", "4.1.3"},
    }
    assert set(expected_by_principle) == set(WCAGPrinciple), "a principle is missing from this test"
    assert set().union(*expected_by_principle.values()) == set(WCAG_22_CRITERIA), (
        "a criterion is missing from every principle set above"
    )
    for principle, expected in expected_by_principle.items():
        assert {sc.sc for sc in get_criteria_by_principle(principle)} == expected, principle


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
    """EVERY entry is a SuccessCriterion, not just the first.

    Stopping at the first entry let a malformed later entry (a bare dict, say) pass
    while other tests raised AttributeError rather than failing cleanly.

    Preconditions:
        None.

    Postconditions:
        Asserts the type of every entry; does not mutate the table.
    """
    wrong = {
        k: type(v).__name__
        for k, v in WCAG_22_CRITERIA.items()
        if not isinstance(v, SuccessCriterion)
    }
    assert wrong == {}, f"entries that are not SuccessCriterion: {wrong}"


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


# WCAG 2.2 added exactly these nine criteria. The table carries all of them.
NEW_IN_22 = {"2.4.11", "2.4.12", "2.4.13", "2.5.7", "2.5.8", "3.2.6", "3.3.7", "3.3.8", "3.3.9"}
# WCAG 2.1 added seventeen; these twelve are its Level A and AA additions, which is
# all the table claims to carry. The five absent (1.3.6, 2.2.6, 2.3.3, 2.5.5, 2.5.6)
# are AAA.
NEW_IN_21_A_AA = {
    "1.3.4",
    "1.3.5",
    "1.4.10",
    "1.4.11",
    "1.4.12",
    "1.4.13",
    "2.1.4",
    "2.5.1",
    "2.5.2",
    "2.5.3",
    "2.5.4",
    "4.1.3",
}


def test_get_new_in_22_criteria_matches_the_specification():
    """The version flags identify the WCAG 2.2 additions, not an arbitrary subset.

    Pins membership rather than a count, so swapping one criterion for another still
    fails. Without this the ``new_in_22`` flags have no coverage at all.

    Preconditions:
        None.

    Postconditions:
        Asserts the accessor's membership; does not mutate the table.
    """
    assert {sc.sc for sc in get_new_in_22_criteria()} == NEW_IN_22


def test_get_new_in_21_criteria_matches_the_a_aa_additions():
    """The 2.1 flags identify that version's Level A and AA additions.

    Preconditions:
        None.

    Postconditions:
        Asserts the accessor's membership; does not mutate the table.
    """
    assert {sc.sc for sc in get_new_in_21_criteria()} == NEW_IN_21_A_AA


def test_get_all_sc_numbers_covers_the_whole_table():
    """Every key is reported, with no duplicates introduced by the accessor.

    Preconditions:
        None.

    Postconditions:
        Asserts the returned numbers; does not mutate the table.
    """
    numbers = get_all_sc_numbers()
    assert set(numbers) == set(WCAG_22_CRITERIA)
    assert len(numbers) == len(set(numbers)), "accessor introduced a duplicate"


def test_get_guideline_criteria_selects_only_that_guideline():
    """Selection is by guideline, and an unknown guideline yields an empty list.

    Preconditions:
        None.

    Postconditions:
        Asserts the accessor's selection; does not mutate the table.
    """
    returned = get_guideline_criteria("2.4")
    assert returned, "guideline 2.4 has entries in this table"
    assert all(sc.guideline == "2.4" for sc in returned)
    assert {sc.sc for sc in returned} == {
        sc.sc for sc in WCAG_22_CRITERIA.values() if sc.guideline == "2.4"
    }
    assert get_guideline_criteria("9.9") == []


def test_guideline_criteria_builds_a_fresh_list_per_call():
    """The uncached accessor's documented invariant: a new list object each call.

    ``get_criteria_by_level`` and friends are ``lru_cache``d and hand back the SAME
    list, so a caller that mutates one corrupts every later reader. This accessor
    documents the opposite guarantee, and nothing else pins it — adding a cache here
    would silently break callers relying on it.

    Preconditions:
        None.

    Postconditions:
        Asserts list identity; does not mutate the table.
    """
    assert get_guideline_criteria("2.4") is not get_guideline_criteria("2.4")


def test_accessors_reject_a_precondition_violation():
    """A documented precondition raises rather than returning a plausible empty result.

    ``get_criteria_by_level("AA")`` returning ``[]`` reads as "no AA criteria exist",
    which a coverage matrix reports as full coverage of nothing.

    Preconditions:
        None.

    Postconditions:
        Asserts that each violation raises; does not mutate the table.
    """
    with pytest.raises(AssertionError):
        get_criteria_by_level("AA")
    with pytest.raises(AssertionError):
        get_criteria_by_principle("operable")
    with pytest.raises(AssertionError):
        get_criterion(111)
    with pytest.raises(AssertionError):
        get_guideline_criteria(2.4)


def test_2_3_1_description_states_both_flash_alternatives():
    """2.3.1 is satisfied by EITHER staying under the flash-count limit OR the flash
    being below the general/red thresholds — a string fix, so only a test that reads
    ``.description`` guards it from regressing silently.

    Preconditions:
        None.

    Postconditions:
        Asserts both alternatives are present; does not mutate the table.
    """
    description = get_criterion("2.3.1").description
    assert "three times" in description, "the flash-count alternative is missing"
    assert "threshold" in description.lower(), "the below-threshold alternative is missing"


def test_3_2_6_description_tests_relative_order_not_visual_location():
    """3.2.6 tests a help mechanism's ORDER relative to other page content, not its
    on-screen position — the two are different claims, and the wrong one describes a
    different (stricter, visually-anchored) requirement than the criterion states.

    Preconditions:
        None.

    Postconditions:
        Asserts the order-based wording and the absence of the superseded
        location-based wording; does not mutate the table.
    """
    description = get_criterion("3.2.6").description
    assert "relative order" in description
    assert "consistent location across pages" not in description
