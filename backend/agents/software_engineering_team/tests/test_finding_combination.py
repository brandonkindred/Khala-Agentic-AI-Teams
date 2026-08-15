"""Tests for fuzzy + proximity finding combination (``combine_findings``).

Covers the generalized combine step: same-category proximity (Python
construct) and same-file similarity (Jaccard) merges, the preserved
side-effects citation signal, the no-cross-category / no-cross-file
invariants, exact-duplicate collapse, determinism, and the threshold knob.
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


def test_same_construct_same_category_merges_even_when_descriptions_differ() -> None:
    """Two findings in one Python function/category merge via proximity alone."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, description="the loop index is off by one", suggestion="fix bound"),
        _issue(line=4, description="return value is now None", suggestion="return the sum"),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].category == "logic"
    assert result[0].start_line == 2
    assert result[0].line == 4
    assert "off by one" in result[0].description
    assert "now None" in result[0].description


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


def test_same_file_similar_descriptions_merge_across_constructs() -> None:
    """Near-identical descriptions in the same file/category merge via similarity."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    issues = [
        _issue(line=2, category="standards", description="bare import `os` at module top"),
        _issue(line=7, category="standards", description="bare import `sys` at module top"),
    ]
    result = combine_findings(issues, index)
    # The quoted identifier is dropped by the tokenizer, so both descriptions
    # tokenize identically (Jaccard 1.0) -> one merged finding.
    assert len(result) == 1
    assert result[0].category == "standards"


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


def test_similarity_threshold_knob_gates_merging() -> None:
    """Two descriptions with ~0.75 Jaccard merge at the default but not at 0.9."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    # Lines 2 (in foo) and 7 (in bar) are different constructs, so only the
    # similarity signal can merge these. Tokens overlap 6/8 -> Jaccard 0.75.
    issues = [
        _issue(line=2, category="standards", description="missing null check on user input value"),
        _issue(
            line=7, category="standards", description="missing null check on user request value"
        ),
    ]
    assert len(combine_findings(issues, index)) == 1  # default 0.6 <= 0.75 -> merge
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
    issues = [
        _issue(line=2, severity="low", description="d one", pre_existing=True),
        _issue(line=4, severity="high", description="d two", pre_existing=False),
    ]
    result = combine_findings(issues, index)
    assert len(result) == 1
    assert result[0].severity == "high"
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


def test_non_python_file_has_no_proximity_but_similarity_still_applies() -> None:
    """Non-Python files get no construct grouping, but similarity still merges near-dupes."""
    ts = "\n".join([f"const x{i} = {i};" for i in range(20)])
    index = _index({"app/foo.ts": ts})
    dissimilar = [
        _issue(file_path="app/foo.ts", line=1, category="logic", description="alpha unrelated"),
        _issue(file_path="app/foo.ts", line=10, category="logic", description="beta different"),
    ]
    assert len(combine_findings(dissimilar, index)) == 2  # no proximity, low similarity
    similar = [
        _issue(
            file_path="app/foo.ts", line=1, category="logic", description="missing await on `a`"
        ),
        _issue(
            file_path="app/foo.ts", line=10, category="logic", description="missing await on `b`"
        ),
    ]
    assert len(combine_findings(similar, index)) == 1  # similarity still merges


def test_fewer_than_two_findings_returns_copy() -> None:
    """A list shorter than two is returned as a shallow copy, unchanged."""
    index = _index({"app/foo.py": _TWO_FUNCS})
    one = [_issue(line=2)]
    result = combine_findings(one, index)
    assert result == one
    assert result is not one
    assert combine_findings([], index) == []
