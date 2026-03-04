"""Tests for the Product Requirements Analysis agent."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent
from product_requirements_analysis_agent.models import (
    AnsweredQuestion,
    OpenQuestion,
    QuestionOption,
    SpecReviewResult,
)


def test_update_spec_writes_versioned_file(tmp_path: Path) -> None:
    """_update_spec with version=7 writes updated_spec_v7.md and updated_spec.md."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v6.md").write_text("# v6")

    llm = MagicMock()
    llm.complete_text.return_value = "# Updated spec content"

    agent = ProductRequirementsAnalysisAgent(llm)
    answered = [
        AnsweredQuestion(
            question_id="q1",
            question_text="Question?",
            selected_answer="Answer",
        )
    ]
    result = agent._update_spec(
        current_spec="# Original",
        answered_questions=answered,
        repo_path=tmp_path,
        version=7,
    )

    assert result == "# Updated spec content"
    v7_file = tmp_path / "plan" / "product_analysis" / "updated_spec_v7.md"
    assert v7_file.exists()
    assert v7_file.read_text() == "# Updated spec content"
    latest = tmp_path / "plan" / "product_analysis" / "updated_spec.md"
    assert latest.exists()
    assert latest.read_text() == "# Updated spec content"


def test_run_workflow_uses_next_version_after_existing_v6(tmp_path: Path) -> None:
    """When plan/product_analysis has updated_spec_v6.md, run_workflow passes version=7 to _update_spec."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v6.md").write_text("# v6")

    one_question = OpenQuestion(
        id="q1",
        question_text="Which framework?",
        options=[QuestionOption(id="opt1", label="React", is_default=True, rationale="", confidence=0.9)],
    )
    spec_review_with_question = SpecReviewResult(
        summary="Review", issues=[], gaps=[], open_questions=[one_question]
    )
    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )

    llm = MagicMock()
    llm.complete_text.return_value = "# Cleaned spec"

    agent = ProductRequirementsAnalysisAgent(llm)
    update_spec_calls = []

    def capture_update_spec(*args, **kwargs):
        update_spec_calls.append(kwargs.get("version") or (args[3] if len(args) > 3 else None))
        return kwargs.get("current_spec", args[0] if args else "") + "\n# Updated"

    agent._update_spec = MagicMock(side_effect=capture_update_spec)

    call_count = [0]

    def run_spec_review(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return spec_review_with_question, kwargs.get("spec_content", args[0] if args else "# Spec")
        return spec_review_no_questions, "# Spec\n# Updated"

    with patch.object(agent, "_communicate_with_user") as mock_comm:
        mock_comm.return_value = [
            AnsweredQuestion(question_id="q1", question_text="Which framework?", selected_answer="React")
        ]
        with patch.object(agent, "_run_spec_review", side_effect=run_spec_review):
            result = agent.run_workflow(
                spec_content="# Spec",
                repo_path=tmp_path,
                job_id="test-job",
                job_updater=lambda **kw: None,
            )

    assert result.success
    assert len(update_spec_calls) >= 1, "_update_spec should be called with version"
    assert update_spec_calls[0] == 7, "First spec update should use version 7 when v6 exists"


def test_run_workflow_renames_validated_spec_when_needs_more_detail(tmp_path: Path) -> None:
    """When input is validated_spec.md and agent has open questions, rename it to updated_spec_v1 then write v2 for update."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    validated = tmp_path / "plan" / "product_analysis" / "validated_spec.md"
    validated.write_text("# Validated content")

    one_question = OpenQuestion(
        id="q1",
        question_text="Which framework?",
        options=[QuestionOption(id="opt1", label="React", is_default=True, rationale="", confidence=0.9)],
    )
    spec_review_with_question = SpecReviewResult(
        summary="Review", issues=[], gaps=[], open_questions=[one_question]
    )
    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )

    llm = MagicMock()
    llm.complete_text.return_value = "# Cleaned spec"

    agent = ProductRequirementsAnalysisAgent(llm)
    update_spec_calls = []

    def capture_update_spec(*args, **kwargs):
        update_spec_calls.append(kwargs.get("version") or (args[3] if len(args) > 3 else None))
        return kwargs.get("current_spec", args[0] if args else "") + "\n# Updated"

    agent._update_spec = MagicMock(side_effect=capture_update_spec)

    call_count = [0]

    def run_spec_review(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return spec_review_with_question, kwargs.get("spec_content", args[0] if args else "# Validated content")
        return spec_review_no_questions, "# Validated content\n# Updated"

    with patch.object(agent, "_communicate_with_user") as mock_comm:
        mock_comm.return_value = [
            AnsweredQuestion(question_id="q1", question_text="Which framework?", selected_answer="React")
        ]
        with patch.object(agent, "_run_spec_review", side_effect=run_spec_review):
            result = agent.run_workflow(
                spec_content="# Validated content",
                repo_path=tmp_path,
                job_id="test-job",
                job_updater=lambda **kw: None,
                initial_spec_path=validated,
            )

    assert result.success
    v1 = tmp_path / "plan" / "product_analysis" / "updated_spec_v1.md"
    assert v1.exists(), "validated_spec should have been renamed to updated_spec_v1.md (before final validated_spec write)"
    assert v1.read_text() == "# Validated content", "v1 should contain the original validated content from the rename"
    assert len(update_spec_calls) >= 1
    assert update_spec_calls[0] == 2, "First _update_spec after rename should use version 2"
