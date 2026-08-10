"""Tests for GitHub issue grooming Phase B sub-issue splitting."""

from __future__ import annotations

from software_engineering_team.github_source.client import Issue
from software_engineering_team.github_source.issue_grooming_scoring import (
    PHASE_B_END,
    PHASE_B_START,
    ScoreBreakdown,
    inject_complexity_block,
)
from software_engineering_team.github_source.issue_grooming_split import (
    MAX_SUB_ISSUES,
    MIN_SPLIT_ITEMS,
    SPLIT_THRESHOLD,
    build_sub_issue,
    extract_checklist_items,
    inject_sub_issues_block,
    plan_sub_issue_items,
    render_sub_issues_block,
    should_split,
)


def _score(aggregate: int) -> ScoreBreakdown:
    return ScoreBreakdown(
        conceptual=aggregate,
        conceptual_rationale="r",
        anticipated_loc=aggregate,
        anticipated_loc_rationale="r",
        solution_complexity=aggregate,
        solution_complexity_rationale="r",
        aggregate=aggregate,
    )


def _parent(number: int = 42, title: str = "Big feature") -> Issue:
    return Issue(
        number=number,
        title=title,
        body="",
        state="open",
        html_url=f"https://example/issues/{number}",
        labels=(),
        id=100000 + number,
    )


# ---------------------------------------------------------------------------
# extract_checklist_items
# ---------------------------------------------------------------------------


class TestExtractChecklistItems:
    def test_extracts_dash_and_star_bullets(self) -> None:
        body = "- [ ] first\n* [ ] second\n- [x] third (done)"
        assert extract_checklist_items(body) == ["first", "second", "third (done)"]

    def test_ignores_plain_bullets(self) -> None:
        body = "- not a checklist item\n- [ ] real item"
        assert extract_checklist_items(body) == ["real item"]

    def test_empty_when_no_checklist(self) -> None:
        assert extract_checklist_items("Just prose, no checklist.") == []

    def test_strips_prior_grooming_blocks_first(self) -> None:
        score = _score(2)
        base = "- [ ] one\n- [ ] two"
        groomed = inject_complexity_block(base, score)
        assert extract_checklist_items(groomed) == ["one", "two"]


# ---------------------------------------------------------------------------
# should_split
# ---------------------------------------------------------------------------


class TestShouldSplit:
    def test_true_when_both_thresholds_met(self) -> None:
        items = ["a", "b", "c"]
        assert len(items) >= MIN_SPLIT_ITEMS
        assert should_split(_score(SPLIT_THRESHOLD), items) is True

    def test_false_when_aggregate_too_low(self) -> None:
        items = ["a", "b", "c", "d"]
        assert should_split(_score(SPLIT_THRESHOLD - 3), items) is False

    def test_false_when_too_few_checklist_items(self) -> None:
        items = ["a", "b"]
        assert len(items) < MIN_SPLIT_ITEMS
        assert should_split(_score(21), items) is False

    def test_false_when_no_checklist_items(self) -> None:
        assert should_split(_score(21), []) is False


# ---------------------------------------------------------------------------
# plan_sub_issue_items
# ---------------------------------------------------------------------------


class TestPlanSubIssueItems:
    def test_returns_unchanged_when_under_cap(self) -> None:
        items = [f"item {i}" for i in range(MAX_SUB_ISSUES)]
        assert plan_sub_issue_items(items) == items

    def test_folds_tail_when_over_cap(self) -> None:
        items = [f"item {i}" for i in range(MAX_SUB_ISSUES + 5)]
        planned = plan_sub_issue_items(items)
        assert len(planned) == MAX_SUB_ISSUES
        assert planned[:-1] == items[: MAX_SUB_ISSUES - 1]
        assert planned[-1].startswith("Remaining items (6): ")
        for tail_item in items[MAX_SUB_ISSUES - 1 :]:
            assert tail_item in planned[-1]

    def test_never_drops_an_item(self) -> None:
        items = [f"item {i}" for i in range(20)]
        planned = plan_sub_issue_items(items)
        recovered = planned[:-1] + planned[-1].split(": ", 1)[1].split("; ")
        assert recovered == items


# ---------------------------------------------------------------------------
# build_sub_issue
# ---------------------------------------------------------------------------


class TestBuildSubIssue:
    def test_title_and_body_shape(self) -> None:
        parent = _parent(number=42, title="Big feature")
        title, body = build_sub_issue(parent, "handle the edge case", index=1, total=3)
        assert title == "Big feature — handle the edge case"
        assert "Split from #42" in body
        assert parent.html_url in body
        assert "(1/3)" in body
        assert "- [ ] handle the edge case" in body

    def test_truncates_long_title_with_ellipsis(self) -> None:
        parent = _parent(title="X" * 200)
        title, _ = build_sub_issue(parent, "short item", index=1, total=1)
        assert len(title) <= 120
        assert title.endswith("— short item")
        assert "…" in title

    def test_caps_title_even_when_item_text_alone_exceeds_title_max(self) -> None:
        parent = _parent(title="Short parent title")
        long_item = "Y" * 200
        title, _ = build_sub_issue(parent, long_item, index=1, total=1)
        assert len(title) <= 120
        assert title.endswith("…")


# ---------------------------------------------------------------------------
# render_sub_issues_block / inject_sub_issues_block
# ---------------------------------------------------------------------------


class TestRenderSubIssuesBlock:
    def test_lists_each_child(self) -> None:
        block = render_sub_issues_block([(43, "Child one"), (44, "Child two")])
        assert block == "## Sub-issues\n- #43 Child one\n- #44 Child two"


class TestInjectSubIssuesBlock:
    def test_appends_and_is_idempotent(self) -> None:
        children = [(43, "Child one")]
        once = inject_sub_issues_block("Description.", children)
        assert (
            once
            == f"Description.\n\n{PHASE_B_START}\n## Sub-issues\n- #43 Child one\n{PHASE_B_END}"
        )
        twice = inject_sub_issues_block(once, [(43, "Child one"), (44, "Child two")])
        assert twice.count(PHASE_B_START) == 1
        assert "Child two" in twice
