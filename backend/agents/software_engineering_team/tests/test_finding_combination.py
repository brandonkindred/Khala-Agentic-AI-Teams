"""Tests for fuzzy + proximity finding combination (``combine_findings``).

Covers the generalized combine step: same-construct proximity (side-effects
always; other categories only when descriptions are also similar), same-anchor
near-duplicate collapse (same line or one unanchored), the preserved
side-effects citation signal, the no-cross-category / no-cross-line / no
-cross-file invariants, determinism, and the threshold knob.
"""

from __future__ import annotations

from code_review_agent.false_positive_filter import CodebaseIndex
from code_review_agent.finding_combination import combine_findings
from code_review_agent.models import CodeReviewIssue


def _issue(**kwargs) -> CodeReviewIssue:
    defaults = dict(
        severity="medium",
        category="logic",
        file_path="app/foo.py",
        line=3,
        description="finding",
        suggestion="",
        pre_existing=False,
    )
    defaults.update(kwargs)
    return CodeReviewIssue(**defaults)


def _index(files: dict) -> CodebaseIndex:
    return CodebaseIndex(files=files)


_TWO_FUNCS = "\n".join(
    [
        "def foo():",  # 1
        "    x = 1",  # 2
        "    y = 2",  # 3
        "    return x + y",  # 4
        "",
        "def bar():",  # 6
        "    return 2",  # 7
        "",
    ]
)


# --------------------------------------------------------------------------- proximity


def test_side_effects_same_construct_merge_regardless_of_wording() -> None:
    """Side-effects findings in one construct merge even with distinct wording
    (multiple symptoms of one change) -- the consolidation rule, generalized."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="side-effects", description="mutates shared state", suggestion="a"),
        _issue(line=4, category="side-effects", description="return type changed", suggestion="b"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].category == "side-effects"
    assert result[0].start_line == 2
    assert result[0].line == 4


def test_non_side_effects_same_construct_merge_only_when_similar() -> None:
    """For non-side-effects, same-construct findings merge only when their
    descriptions are also similar: two distinct bugs in one function stay
    separate; two near-identical ones combine."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    distinct = [
        _issue(line=2, category="logic", description="off-by-one in the loop bound"),
        _issue(line=4, category="logic", description="returns None instead of the sum"),
    ]
    assert len(combine_findings(distinct, index)) == 2  # same function, different bugs
    similar = [
        _issue(line=2, category="logic", description="unchecked index access on `a`"),
        _issue(line=4, category="logic", description="unchecked index access on `b`"),
    ]
    assert len(combine_findings(similar, index)) == 1  # same function, same issue


def test_same_construct_different_category_does_not_merge() -> None:
    """Proximity never crosses a category boundary."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="logic", description="logic problem"),
        _issue(line=3, category="naming", description="poor name"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 2


def test_different_constructs_dissimilar_do_not_merge() -> None:
    """Distinct functions with unrelated descriptions stay separate."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, description="alpha problem in foo"),
        _issue(line=7, description="beta concern in bar"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 2


# --------------------------------------------------------------------------- similarity


def test_cross_construct_similar_descriptions_do_not_merge() -> None:
    """Similar findings in DIFFERENT constructs (different lines) are reported
    individually, never merged -- collapsing separate occurrences would lose an
    inline-comment anchor. The systemic pass themes them instead."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="standards", description="bare import `os` at module top"),
        _issue(line=7, category="standards", description="bare import `sys` at module top"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 2


def test_same_file_dissimilar_descriptions_do_not_merge() -> None:
    """Same file/category but low Jaccard overlap stays separate."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="standards", description="prefer f-strings over percent format"),
        _issue(line=7, category="standards", description="unreachable branch after early return"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 2


def test_similarity_does_not_cross_files() -> None:
    """Identical descriptions in different files are NOT merged (would lose anchors)."""
    index = _index({"app/foo.py": _TWO_FUNCS, "app/bar.py": _TWO_FUNCS})
    issues = [
        _issue(
            file_path="app/foo.py", line=2, category="standards", description="bare import here"
        ),
        _issue(
            file_path="app/bar.py", line=2, category="standards", description="bare import here"
        ),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 2


def test_similarity_threshold_knob_gates_same_construct_merge() -> None:
    """Two non-side-effects findings in one construct with ~0.75 Jaccard merge at
    the default threshold but not at 0.9."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    # Lines 2 and 3 are both inside foo() (same construct); tokens overlap 6/8
    # -> Jaccard 0.75, so only the threshold decides whether they merge.
    issues = [
        _issue(line=2, category="logic", description="missing null check on user input value"),
        _issue(line=3, category="logic", description="missing null check on user request value"),
    ]
    assert len(combine_findings(issues, index)) == 1  # 0.6 <= 0.75 -> merge
    assert len(combine_findings(issues, index, similarity_threshold=0.9)) == 2  # 0.9 > 0.75


# --------------------------------------------------------------------------- exact duplicates / anchors


def test_exact_duplicate_findings_collapse() -> None:
    """An exact duplicate (same file/line/description) collapses to one."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, description="identical defect"),
        _issue(line=2, description="identical defect"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].line == 2


def test_both_unanchored_file_level_findings_do_not_merge() -> None:
    """Two file-level findings (both line=None) in one file are distinct
    observations and must each survive, even when their descriptions tokenize
    alike (e.g. two unmet acceptance criteria differing only by numbers, which
    the digit-dropping tokenizer collapses)."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(
            line=None, category="spec-compliance", description="add(1, 2) returns 3 :: no evidence"
        ),
        _issue(
            line=None, category="spec-compliance", description="add(0, 0) returns 0 :: no evidence"
        ),
    ]
    assert len(combine_findings(issues, index)) == 2


def test_unanchored_copy_merges_into_anchored_and_keeps_anchor() -> None:
    """An unanchored (line=None) copy folds into its anchored twin, preserving the anchor."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, description="same defect text"),
        _issue(line=None, description="same defect text"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].line == 2


# --------------------------------------------------------------------------- side-effects citation signal


def test_side_effects_citation_merges_across_files() -> None:
    """The side-effects citation link still merges the 'both ends' case across files."""
    foo = "\n".join(["def foo():", "    return 1", ""])  # app/foo.py:1-2
    bar = "\n".join(["def bar():", "    return foo() + 1", ""])  # app/bar.py:1-2
    index = _index({"app/foo.py": foo, "app/bar.py": bar})
    issues = [
        _issue(
            file_path="app/foo.py",
            line=2,
            category="side-effects",
            description="foo's return value changed",
        ),
        _issue(
            file_path="app/bar.py",
            line=2,
            category="side-effects",
            description="bar breaks because of app/foo.py:2 (foo's old contract)",
        ),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].category == "side-effects"


def test_consolidate_side_effects_false_disables_side_effect_specialcasing() -> None:
    """With ``consolidate_side_effects=False`` (the escape hatch), side-effects
    findings get no special treatment: two in one construct with distinct wording
    no longer merge (they would with it on)."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="side-effects", description="mutates shared state"),
        _issue(line=4, category="side-effects", description="return type changed"),
    ]
    assert len(combine_findings(issues, index)) == 1  # default on -> consolidated
    assert len(combine_findings(issues, index, consolidate_side_effects=False)) == 2  # off


def test_non_side_effects_citation_does_not_merge_across_files() -> None:
    """The citation link is confined to side-effects; other categories do not cross files."""
    foo = "\n".join(["def foo():", "    return 1", ""])
    bar = "\n".join(["def bar():", "    return foo() + 1", ""])
    index = _index({"app/foo.py": foo, "app/bar.py": bar})
    issues = [
        _issue(file_path="app/foo.py", line=2, category="logic", description="foo return changed"),
        _issue(
            file_path="app/bar.py",
            line=2,
            category="logic",
            description="bar breaks because of app/foo.py:2",
        ),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 2


# --------------------------------------------------------------------------- merge semantics / invariants


def test_merge_takes_max_severity_and_ands_pre_existing() -> None:
    """Merged severity is the group max; pre_existing is True only if all members are."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    # Side-effects in one construct merge regardless of wording, so this isolates
    # the severity/pre_existing merge semantics.
    issues = [
        _issue(
            line=2, category="side-effects", severity="low", description="one", pre_existing=True
        ),
        _issue(
            line=4, category="side-effects", severity="high", description="two", pre_existing=False
        ),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].severity == "high"
    assert result[0].pre_existing is False


def test_merge_ors_omission() -> None:
    """Merged omission is True if ANY member is (OR across the group -- the
    inverse quantifier from pre_existing's AND, see
    side_effect_consolidation._merge_group's docstring)."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="side-effects", description="one", omission=True),
        _issue(line=4, category="side-effects", description="two", omission=False),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].omission is True
    assert result[0].pre_existing is False


def test_ungrouped_finding_keeps_position() -> None:
    """A standalone finding keeps its relative position among a merged group."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="logic", description="a defect in foo"),
        _issue(line=7, category="naming", description="standalone naming nit"),
        _issue(line=4, category="logic", description="another defect in foo"),
    ]
    result = combine_findings(issues, index)
    # The two logic findings (foo function) merge; the naming nit stays put.
    assert len(result) == 2
    assert result[0].category == "logic"
    assert result[1].category == "naming"


def test_non_python_same_anchor_duplicate_merges_but_different_lines_do_not() -> None:
    """Non-Python files get no construct proximity. Same-line near-duplicates
    still collapse (the same-anchor rule); similar findings on different lines
    are kept separate."""
    ts = "\n".join([f"const x{i} = {i};" for i in range(20)])
    index = _index({"app/foo.ts": ts})
    different_lines = [
        _issue(
            file_path="app/foo.ts", line=1, category="logic", description="missing await on `a`"
        ),
        _issue(
            file_path="app/foo.ts", line=10, category="logic", description="missing await on `b`"
        ),
    ]
    assert len(combine_findings(different_lines, index)) == 2  # no proximity, different anchors
    same_line = [
        _issue(
            file_path="app/foo.ts", line=5, category="logic", description="missing await on `a`"
        ),
        _issue(
            file_path="app/foo.ts", line=5, category="logic", description="missing await on `b`"
        ),
    ]
    assert len(combine_findings(same_line, index)) == 1  # same-anchor near-duplicate


def test_fail_open_on_unreadable_or_unparseable_files() -> None:
    """Fail-open contract: findings that cite files absent from the index, or
    whose content will not parse, contribute no construct/proximity signal and
    combine_findings degrades gracefully (same-anchor de-duplication still
    applies) rather than raising."""
    # (a) file not in the index -> no construct resolvable; different lines stay
    #     separate, and nothing raises.
    idx_missing = _index({"app/present.py": _TWO_FUNCS})
    absent = [
        _issue(file_path="app/missing.py", line=5, category="logic", description="alpha finding"),
        _issue(file_path="app/missing.py", line=9, category="logic", description="beta finding"),
    ]
    assert len(combine_findings(absent, idx_missing)) == 2

    # (b) unparseable Python content -> construct resolution yields nothing, but
    #     the same-anchor rule (same line, similar wording) still de-dupes, and
    #     no exception escapes.
    idx_broken = _index({"app/broken.py": "def (:\n    x = 1\n"})
    broken = [
        _issue(file_path="app/broken.py", line=2, category="logic", description="unchecked access"),
        _issue(file_path="app/broken.py", line=2, category="logic", description="unchecked access"),
    ]
    assert len(combine_findings(broken, idx_broken)) == 1


def test_fewer_than_two_findings_returns_copy() -> None:
    """A list shorter than two is returned as a shallow copy, unchanged."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    one = [_issue(line=2)]
    result = combine_findings(one, index)
    assert result == one
    assert result is not one
    assert combine_findings([], index) == []
