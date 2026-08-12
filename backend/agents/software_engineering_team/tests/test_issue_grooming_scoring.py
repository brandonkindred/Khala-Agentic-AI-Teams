"""Tests for the heuristic Fibonacci complexity scorer (GitHub issue grooming Phase A)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from software_engineering_team.github_source.issue_grooming_scoring import (
    FIBONACCI,
    PHASE_A_END,
    PHASE_A_START,
    ScoreBreakdown,
    complexity_label,
    from_unified_score,
    inject_complexity_block,
    inject_marked_block,
    merge_complexity_label,
    nearest_fibonacci,
    render_complexity_markdown,
    score_issue,
    strip_grooming_blocks,
)
from software_engineering_team.github_source.issue_scoring import (
    ScoreBreakdown as UnifiedScoreBreakdown,
)

# ---------------------------------------------------------------------------
# nearest_fibonacci
# ---------------------------------------------------------------------------


class TestNearestFibonacci:
    @pytest.mark.parametrize("n", [-5, 0, 1])
    def test_clamps_below_range(self, n: int) -> None:
        assert nearest_fibonacci(n) == FIBONACCI[0]

    @pytest.mark.parametrize("n", [21, 30, 1000])
    def test_clamps_above_range(self, n: int) -> None:
        assert nearest_fibonacci(n) == FIBONACCI[-1]

    @pytest.mark.parametrize("n,expected", [(2, 2), (3, 3), (5, 5), (8, 8), (13, 13)])
    def test_exact_members_are_stable(self, n: int, expected: int) -> None:
        assert nearest_fibonacci(n) == expected

    @pytest.mark.parametrize(
        "n,expected",
        [
            (4, 5),  # tie between 3 and 5 -> rounds up
            (17, 21),  # tie between 13 and 21 -> rounds up
            (6, 5),  # closer to 5 than 8
            (7, 8),  # closer to 8 than 5
            (10, 8),  # closer to 8 than 13
            (11, 13),  # closer to 13 than 8
        ],
    )
    def test_nearest_and_ties_round_up(self, n: int, expected: int) -> None:
        assert nearest_fibonacci(n) == expected


# ---------------------------------------------------------------------------
# score_issue dimension thresholds
# ---------------------------------------------------------------------------


class TestScoreIssueConceptual:
    def test_no_keywords_scores_minimum(self) -> None:
        score = score_issue("Fix a typo", "Change 'teh' to 'the' in the README.")
        assert score.conceptual == 1
        assert "no complexity keyword signals found" in score.conceptual_rationale

    def test_single_weak_keyword(self) -> None:
        score = score_issue("Add schema field", "Add a new column to the schema.")
        assert score.conceptual == 1  # weight 1 -> nearest_fibonacci(1) == 1
        assert "schema" in score.conceptual_rationale

    def test_multiple_keywords_increase_score(self) -> None:
        score = score_issue(
            "Auth migration",
            "This is a breaking change requiring a database migration and new authentication architecture.",
        )
        assert score.conceptual >= 8
        for kw in ("breaking change", "migration", "authentication", "architecture"):
            assert kw in score.conceptual_rationale


class TestScoreIssueAnticipatedLoc:
    def test_short_body_no_files_scores_minimum(self) -> None:
        score = score_issue("T", "Short body.")
        assert score.anticipated_loc == 1

    def test_body_with_several_file_references(self) -> None:
        body = " ".join(f"`backend/module_{i}.py`" for i in range(4)) + " " + ("word " * 100)
        score = score_issue("T", body)
        assert score.anticipated_loc >= 3

    def test_dotted_method_calls_are_not_counted_as_file_references(self) -> None:
        body = "See `self.foo` and `json.dumps` for context. " + ("word " * 100)
        score = score_issue("T", body)
        assert "0 file reference(s)" in score.anticipated_loc_rationale

    def test_medium_body_scores_five(self) -> None:
        score = score_issue("T", "word " * 500)
        assert score.anticipated_loc == 5

    def test_longer_body_scores_eight(self) -> None:
        score = score_issue("T", "word " * 1000)
        assert score.anticipated_loc == 8

    def test_very_long_body_scores_high(self) -> None:
        score = score_issue("T", "word " * 2500)
        assert score.anticipated_loc == 13

    def test_extremely_long_body_scores_max(self) -> None:
        score = score_issue("T", "word " * 5000)
        assert score.anticipated_loc == 21


class TestScoreIssueSolutionComplexity:
    def test_no_checklist_or_refs_scores_minimum(self) -> None:
        score = score_issue("T", "No structure here.")
        assert score.solution_complexity == 1

    def test_checklist_items_increase_score(self) -> None:
        body = "\n".join(f"- [ ] item {i}" for i in range(6))
        score = score_issue("T", body)
        assert score.solution_complexity >= 3

    def test_many_checklist_items_score_eight(self) -> None:
        body = "\n".join(f"- [ ] item {i}" for i in range(11))
        score = score_issue("T", body)
        assert score.solution_complexity == 8

    def test_very_many_checklist_items_score_thirteen(self) -> None:
        body = "\n".join(f"- [ ] item {i}" for i in range(18))
        score = score_issue("T", body)
        assert score.solution_complexity == 13

    def test_cross_referenced_issues_counted(self) -> None:
        body = "See #101, #102, #103 for context."
        score = score_issue("T", body)
        assert score.solution_complexity >= 2


class TestScoreIssueAggregate:
    def test_aggregate_is_nearest_fibonacci_of_max_dims(self) -> None:
        score = score_issue(
            "Auth migration",
            "This is a breaking change requiring a database migration.\n" + "word " * 50,
        )
        assert score.aggregate == nearest_fibonacci(
            max(score.conceptual, score.anticipated_loc, score.solution_complexity)
        )

    def test_every_dimension_and_aggregate_is_a_fibonacci_member(self) -> None:
        score = score_issue(
            "Auth migration", "breaking change migration architecture " + "word " * 300
        )
        for value in (
            score.conceptual,
            score.anticipated_loc,
            score.solution_complexity,
            score.aggregate,
        ):
            assert value in FIBONACCI

    def test_deterministic(self) -> None:
        title, body = (
            "Add feature",
            "Some body with `a/b.py` and #12 referenced.\n- [ ] one\n- [ ] two",
        )
        assert score_issue(title, body) == score_issue(title, body)

    def test_strips_prior_grooming_blocks_before_scoring(self) -> None:
        base_body = "A small, simple change."
        first = score_issue("T", base_body)
        groomed_body = inject_complexity_block(base_body, first)
        second = score_issue("T", groomed_body)
        assert second == first


# ---------------------------------------------------------------------------
# render_complexity_markdown / inject_marked_block / inject_complexity_block
# ---------------------------------------------------------------------------


def _sample_score() -> ScoreBreakdown:
    return ScoreBreakdown(
        conceptual=2,
        conceptual_rationale="1 keyword signal(s): schema",
        anticipated_loc=2,
        anticipated_loc_rationale="~80 word(s), 1 file reference(s)",
        solution_complexity=2,
        solution_complexity_rationale="2 checklist item(s), 0 cross-referenced issue(s)",
        aggregate=2,
    )


class TestScoreBreakdownInvariants:
    def test_valid_score_constructs(self) -> None:
        _sample_score()  # must not raise

    def test_rejects_non_fibonacci_dimension(self) -> None:
        with pytest.raises(ValidationError):
            ScoreBreakdown(
                conceptual=4,
                conceptual_rationale="r",
                anticipated_loc=2,
                anticipated_loc_rationale="r",
                solution_complexity=2,
                solution_complexity_rationale="r",
                aggregate=4,
            )

    def test_rejects_aggregate_not_matching_nearest_fibonacci_of_max(self) -> None:
        with pytest.raises(ValidationError):
            ScoreBreakdown(
                conceptual=2,
                conceptual_rationale="r",
                anticipated_loc=8,
                anticipated_loc_rationale="r",
                solution_complexity=2,
                solution_complexity_rationale="r",
                aggregate=2,  # should be 8 (max of dims)
            )


# ---------------------------------------------------------------------------
# from_unified_score
# ---------------------------------------------------------------------------


def _unified_score(
    conceptual_score: int = 2,
    loc_score: int = 2,
    code_complexity_score: int = 2,
    aggregate_score: int = 2,
) -> UnifiedScoreBreakdown:
    return UnifiedScoreBreakdown(
        conceptual_score=conceptual_score,
        conceptual_rationale="Unified: conceptual rationale.",
        loc_score=loc_score,
        loc_rationale="Unified: loc rationale.",
        code_complexity_score=code_complexity_score,
        code_complexity_rationale="Unified: code complexity rationale.",
        aggregate_score=aggregate_score,
        aggregate_rationale="Unified: aggregate rationale.",
        suggested_labels=["needs-design"],
    )


class TestFromUnifiedScore:
    def test_maps_fields_and_rationales(self) -> None:
        unified = _unified_score(
            conceptual_score=3, loc_score=5, code_complexity_score=8, aggregate_score=8
        )

        adapted = from_unified_score(unified)

        assert adapted.conceptual == 3
        assert adapted.conceptual_rationale == "Unified: conceptual rationale."
        assert adapted.anticipated_loc == 5
        assert adapted.anticipated_loc_rationale == "Unified: loc rationale."
        assert adapted.solution_complexity == 8
        assert adapted.solution_complexity_rationale == "Unified: code complexity rationale."

    def test_aggregate_is_recomputed_via_max_not_copied_from_input(self) -> None:
        # aggregate_score (1) deliberately disagrees with nearest_fibonacci(max(dims))
        # (8) -- from_unified_score must recompute, never trust the input's own
        # aggregate_score.
        unified = _unified_score(
            conceptual_score=8, loc_score=1, code_complexity_score=1, aggregate_score=1
        )

        adapted = from_unified_score(unified)

        assert adapted.aggregate == 8
        assert adapted.aggregate != unified.aggregate_score

    def test_drops_fields_with_no_legacy_equivalent(self) -> None:
        dumped = from_unified_score(_unified_score()).model_dump()
        assert set(dumped) == {
            "conceptual",
            "conceptual_rationale",
            "anticipated_loc",
            "anticipated_loc_rationale",
            "solution_complexity",
            "solution_complexity_rationale",
            "aggregate",
        }

    @pytest.mark.parametrize("value", [1, 2, 3, 5, 8, 13])
    def test_every_legal_unified_score_value_adapts_without_error(self, value: int) -> None:
        # issue_scoring.FIBONACCI_COMPLEXITY_VALUES is a proper subset of this
        # module's FIBONACCI, so every legal unified score value must adapt
        # cleanly and satisfy this module's own validator.
        adapted = from_unified_score(_unified_score(value, value, value, value))
        assert adapted.aggregate == nearest_fibonacci(value)


class TestRenderComplexityMarkdown:
    def test_matches_expected_shape(self) -> None:
        rendered = render_complexity_markdown(_sample_score())
        assert rendered == (
            "## Complexity (Fibonacci)\n"
            "\n"
            "| Dimension | Score | Rationale |\n"
            "| --- | ---: | --- |\n"
            "| Conceptual | 2 | 1 keyword signal(s): schema |\n"
            "| Anticipated LOC | 2 | ~80 word(s), 1 file reference(s) |\n"
            "| Solution complexity | 2 | 2 checklist item(s), 0 cross-referenced issue(s) |\n"
            "| **Aggregate** | **2** | nearest Fibonacci of max(dims) |\n"
            "\n"
            "## Conceptual complexity\n"
            "Conceptual=2, anticipated LOC=2, solution complexity=2; aggregate Fibonacci score **2**."
        )


class TestInjectMarkedBlock:
    def test_appends_to_non_blank_body(self) -> None:
        result = inject_marked_block("Original description.", PHASE_A_START, PHASE_A_END, "BLOCK")
        assert result == f"Original description.\n\n{PHASE_A_START}\nBLOCK\n{PHASE_A_END}"

    def test_wraps_blank_body_with_no_leading_separator(self) -> None:
        result = inject_marked_block("", PHASE_A_START, PHASE_A_END, "BLOCK")
        assert result == f"{PHASE_A_START}\nBLOCK\n{PHASE_A_END}"

    def test_replaces_in_place_on_second_injection(self) -> None:
        once = inject_marked_block("Description.", PHASE_A_START, PHASE_A_END, "BLOCK ONE")
        twice = inject_marked_block(once, PHASE_A_START, PHASE_A_END, "BLOCK TWO")
        assert twice == f"Description.\n\n{PHASE_A_START}\nBLOCK TWO\n{PHASE_A_END}"
        assert twice.count(PHASE_A_START) == 1
        assert "BLOCK ONE" not in twice

    def test_preserves_content_after_the_block(self) -> None:
        body = f"Before.\n\n{PHASE_A_START}\nOLD\n{PHASE_A_END}\n\nAfter."
        result = inject_marked_block(body, PHASE_A_START, PHASE_A_END, "NEW")
        assert result == f"Before.\n\n{PHASE_A_START}\nNEW\n{PHASE_A_END}\n\nAfter."

    def test_rejects_block_containing_start_marker(self) -> None:
        with pytest.raises(ValueError, match="start_marker"):
            inject_marked_block("Description.", PHASE_A_START, PHASE_A_END, f"oops {PHASE_A_START}")

    def test_rejects_block_containing_end_marker(self) -> None:
        with pytest.raises(ValueError, match="end_marker"):
            inject_marked_block("Description.", PHASE_A_START, PHASE_A_END, f"oops {PHASE_A_END}")


class TestInjectComplexityBlock:
    def test_idempotent_replace_not_duplicate(self) -> None:
        score = _sample_score()
        once = inject_complexity_block("Description.", score)
        twice = inject_complexity_block(once, score)
        assert once == twice
        assert twice.count(PHASE_A_START) == 1


class TestStripGroomingBlocks:
    def test_strips_phase_a_block(self) -> None:
        score = _sample_score()
        injected = inject_complexity_block("Original text.", score)
        assert strip_grooming_blocks(injected) == "Original text."

    def test_no_op_when_no_blocks_present(self) -> None:
        assert strip_grooming_blocks("Plain body.") == "Plain body."

    def test_symmetric_with_injection_round_trip(self) -> None:
        score = _sample_score()
        original = "Some original description spanning\nmultiple lines."
        injected = inject_complexity_block(original, score)
        assert strip_grooming_blocks(injected) == original

    def test_preserves_content_on_both_sides_of_the_block(self) -> None:
        body = f"Before text.\n\n{PHASE_A_START}\nSTALE BLOCK\n{PHASE_A_END}\n\nAfter text."
        assert strip_grooming_blocks(body) == "Before text.\n\nAfter text."


# ---------------------------------------------------------------------------
# complexity_label / merge_complexity_label
# ---------------------------------------------------------------------------


class TestComplexityLabel:
    def test_renders_aggregate(self) -> None:
        assert complexity_label(_sample_score()) == "complexity: 2"


class TestMergeComplexityLabel:
    def test_appends_when_no_prior_label(self) -> None:
        result = merge_complexity_label(("bug", "enhancement"), _sample_score())
        assert result == ["bug", "enhancement", "complexity: 2"]

    def test_replaces_prior_complexity_label(self) -> None:
        result = merge_complexity_label(("bug", "complexity: 8"), _sample_score())
        assert result == ["bug", "complexity: 2"]

    def test_replaces_prior_label_regardless_of_position(self) -> None:
        result = merge_complexity_label(("complexity: 13", "bug"), _sample_score())
        assert result == ["bug", "complexity: 2"]
