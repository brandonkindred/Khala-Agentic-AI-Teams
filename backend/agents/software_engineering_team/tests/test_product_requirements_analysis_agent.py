"""Tests for the Product Requirements Analysis agent."""

import json
import logging
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest
from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent
from product_requirements_analysis_agent.agent import (
    MAX_GAP_ROUNDS,
)
from product_requirements_analysis_agent.models import (
    AnalysisPhase,
    AnalysisWorkflowResult,
    AnsweredQuestion,
    ArchitectureAnalysisResult,
    OpenQuestion,
    QuestionOption,
    SOPDecision,
    SOPSubPhase,
    SpecCleanupResult,
    SpecReviewResult,
    ToolGapAnalysis,
    ToolRecommendation,
)
from product_requirements_analysis_agent.qa_history import extract_answer_from_qa_history
from product_requirements_analysis_agent.question_data import (
    SOP_PHASE1_QUESTIONS,
    _sop_phase1_fallback_questions,
    context_discovery_fallback_questions,
)
from product_requirements_analysis_agent.question_processing import (
    _safe_bool,
    _stem_candidates,
    filter_duplicate_questions,
    parse_question_option,
    parse_spec_review_response,
)
from product_requirements_analysis_agent.question_processing import (
    parse_open_question as _real_parse_open_question,
)

from llm_service.clients.dummy import DummyLLMClient


class _StubClientBase(DummyLLMClient):
    """Shared base: returns a fixed canned response from ``complete_json``.

    Routes transparently through the Strands adapter path
    (``stream()`` → ``complete_json`` override below). For PRA tests,
    this replaces the pre-migration ``MagicMock().complete_json.return_value = {...}``
    pattern. When the response is a dict, ``stream()`` JSON-serializes it so the
    Strands Agent returns JSON text that calling code can parse. When the response
    is a string, ``stream()`` passes it through as-is (for prompts expecting
    plain markdown/text).

    Preconditions: none.
    Postconditions: ``complete_json`` always returns the ``response`` passed to
        ``__init__``, regardless of ``prompt``/``system_prompt``/other arguments.
    """

    def __init__(self, response) -> None:
        super().__init__()
        self._response = response

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        return self._response


class _StubClient(_StubClientBase):
    """Returns a canned response for every ``complete_json`` call (untracked)."""


class _TrackingStubClient(_StubClientBase):
    """Returns a canned response and tracks calls for assertions.

    Supports call_count, last_prompt, and all_prompts for tests that
    previously inspected ``llm.complete_json.call_count`` or ``call_args``.

    Postconditions (in addition to the base class's): each ``complete_json`` call
    increments ``call_count`` by 1, sets ``last_prompt`` to that call's ``prompt``,
    and appends ``prompt`` to ``all_prompts``, before returning the base class's
    canned response.
    """

    def __init__(self, response) -> None:
        super().__init__(response)
        self.call_count = 0
        self.last_prompt: Optional[str] = None
        self.all_prompts: list = []

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.call_count += 1
        self.last_prompt = prompt
        self.all_prompts.append(prompt)
        return super().complete_json(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
            tools=tools,
            think=think,
            **kwargs,
        )


def test_format_answered_questions_for_prompt_empty() -> None:
    """_format_answered_questions_for_prompt returns empty string for empty list."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    assert agent._format_answered_questions_for_prompt([]) == ""


def test_format_answered_questions_for_prompt_one_question() -> None:
    """_format_answered_questions_for_prompt formats one AnsweredQuestion in qa_history style."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    aq = AnsweredQuestion(
        question_id="q1",
        question_text="What deployment target?",
        selected_answer="Cloud (AWS)",
        rationale="Best for scale",
    )
    out = agent._format_answered_questions_for_prompt([aq])
    assert "### What deployment target?" in out
    assert "**Answer:** Cloud (AWS)" in out
    assert "**Rationale:** Best for scale" in out


def test_format_answered_questions_for_prompt_multiple_and_optional_fields() -> None:
    """_format_answered_questions_for_prompt produces multiple ### blocks and handles optional fields."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    aq1 = AnsweredQuestion(
        question_id="q1",
        question_text="First question?",
        selected_answer="Yes",
        was_auto_answered=True,
        confidence=0.85,
    )
    aq2 = AnsweredQuestion(
        question_id="q2",
        question_text="Second question?",
        selected_answer="No",
        was_default=True,
        other_text="Custom note",
    )
    out = agent._format_answered_questions_for_prompt([aq1, aq2])
    assert "### First question?" in out
    assert "**Answer:** Yes" in out
    assert "Auto-answered" in out
    assert "85%" in out
    assert "### Second question?" in out
    assert "**Answer:** No" in out
    assert "Default applied" in out
    assert "Custom text:" in out
    assert "Custom note" in out


# --- _has_existing_pra_artifacts ---


def test_has_existing_pra_artifacts_true_when_qa_history_substantive(tmp_path: Path) -> None:
    """_has_existing_pra_artifacts returns True when qa_history.md has length > 200 and contains '## Iteration' and '**Answer:**'."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    qa = tmp_path / "plan" / "product_analysis" / "qa_history.md"
    content = (
        "# Q&A History\n\n## Iteration 1\n\n### OAuth provider?\n**Answer:** GitHub\n\n" + "x" * 200
    )
    qa.write_text(content)
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    assert agent._has_existing_pra_artifacts(tmp_path) is True


def test_has_existing_pra_artifacts_true_when_validated_spec_exists(tmp_path: Path) -> None:
    """_has_existing_pra_artifacts returns True when plan/product_analysis/validated_spec.md exists."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "validated_spec.md").write_text("# Validated")
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    assert agent._has_existing_pra_artifacts(tmp_path) is True


def test_has_existing_pra_artifacts_false_when_dir_empty(tmp_path: Path) -> None:
    """_has_existing_pra_artifacts returns False when plan/product_analysis exists but has no qa_history/validated_spec/updated_spec*."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    assert agent._has_existing_pra_artifacts(tmp_path) is False


def test_has_existing_pra_artifacts_false_when_dir_missing(tmp_path: Path) -> None:
    """_has_existing_pra_artifacts returns False when plan/product_analysis does not exist."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    assert agent._has_existing_pra_artifacts(tmp_path) is False


def test_run_spec_review_invokes_llm_once(tmp_path: Path) -> None:
    """_run_spec_review performs a single LLM call (whole-spec review, no chunking)."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    llm = _TrackingStubClient(
        {
            "issues": [],
            "gaps": [],
            "open_questions": [],
            "summary": "Done",
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    agent._context_files = {}
    result, updated_spec = agent._run_spec_review(
        spec_content="# My Spec\n\n## Section\nContent",
        repo_path=tmp_path,
        answered_questions=None,
    )
    assert llm.call_count == 1
    assert result.summary == "Done"
    assert updated_spec == "# My Spec\n\n## Section\nContent"


def test_run_spec_review_includes_qa_in_prompt(tmp_path: Path) -> None:
    """When answered_questions is non-empty, the prompt passed to the LLM contains Q&A text."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    llm = _TrackingStubClient(
        {
            "issues": [],
            "gaps": [],
            "open_questions": [],
            "summary": "Done",
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    agent._context_files = {}
    answered = [
        AnsweredQuestion(
            question_id="aq1",
            question_text="Where to deploy?",
            selected_answer="Kubernetes",
        )
    ]
    agent._run_spec_review(
        spec_content="# Spec",
        repo_path=tmp_path,
        answered_questions=answered,
    )
    prompt = llm.last_prompt
    assert "Where to deploy?" in prompt
    assert "Kubernetes" in prompt
    assert "Previously Answered" in prompt or "Current session answers" in prompt


def test_run_spec_review_includes_qa_file_in_prompt(tmp_path: Path) -> None:
    """When qa_history.md exists, the prompt passed to the LLM contains its content."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    qa_file = tmp_path / "plan" / "product_analysis" / "qa_history.md"
    qa_file.write_text(
        "# Q&A History\n\n## Iteration 1\n\n### OAuth provider?\n**Answer:** GitHub\n\n"
    )
    llm = _TrackingStubClient(
        {
            "issues": [],
            "gaps": [],
            "open_questions": [],
            "summary": "Done",
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    agent._context_files = {}
    agent._run_spec_review(
        spec_content="# Spec",
        repo_path=tmp_path,
        answered_questions=None,
    )
    prompt = llm.last_prompt
    assert "OAuth provider?" in prompt
    assert "GitHub" in prompt


def test_update_spec_writes_versioned_file(tmp_path: Path) -> None:
    """_update_spec with version=7 writes updated_spec_v7.md and updated_spec.md."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v6.md").write_text("# v6")

    llm = _StubClient("# Updated spec content")

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
        options=[
            QuestionOption(id="opt1", label="React", is_default=True, rationale="", confidence=0.9)
        ],
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
            return spec_review_with_question, kwargs.get(
                "spec_content", args[0] if args else "# Spec"
            )
        return spec_review_no_questions, "# Spec\n# Updated"

    with ExitStack() as stack:
        mock_comm = stack.enter_context(patch.object(agent, "_communicate_with_user"))
        mock_comm.return_value = [
            AnsweredQuestion(
                question_id="q1", question_text="Which framework?", selected_answer="React"
            )
        ]
        stack.enter_context(patch.object(agent, "_run_spec_review", side_effect=run_spec_review))
        stack.enter_context(patch.object(agent, "_run_sop_phase1", return_value=([], "# Spec", [])))
        stack.enter_context(
            patch.object(
                agent, "_run_sop_phase2_architecture", return_value=(MagicMock(), "# Spec")
            )
        )
        stack.enter_context(
            patch.object(
                agent,
                "_run_spec_cleanup",
                return_value=SpecCleanupResult(
                    is_valid=True,
                    validation_issues=[],
                    cleaned_spec="# Cleaned",
                    summary="Done",
                ),
            )
        )
        stack.enter_context(patch.object(agent, "_generate_prd_document", return_value="# PRD"))
        result = agent.run_workflow(
            spec_content="# Spec",
            repo_path=tmp_path,
            job_id="test-job",
            job_updater=lambda **kw: None,
        )

    assert result.success
    assert len(update_spec_calls) >= 1, "_update_spec should be called with version"
    assert update_spec_calls[0] == 7, "First spec update should use version 7 when v6 exists"


def test_run_workflow_re_runs_spec_review_after_clarification(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When _run_spec_review returns a different spec (clarification), re-run spec review on clarified spec and log it."""
    caplog.set_level(logging.INFO)
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)

    one_question = OpenQuestion(
        id="q1",
        question_text="Which OAuth provider?",
        options=[
            QuestionOption(id="opt1", label="GitHub", is_default=True, rationale="", confidence=0.9)
        ],
    )
    spec_review_with_question = SpecReviewResult(
        summary="Review", issues=[], gaps=[], open_questions=[one_question]
    )
    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )
    cleanup_result = SpecCleanupResult(
        is_valid=True, validation_issues=[], cleaned_spec="# Cleaned", summary="Done"
    )

    llm = MagicMock()
    llm.complete_text.return_value = "# Cleaned spec"
    agent = ProductRequirementsAnalysisAgent(llm)

    run_spec_review_calls = []

    def run_spec_review(spec_content, *args, **kwargs):
        run_spec_review_calls.append(spec_content)
        if len(run_spec_review_calls) == 1:
            return spec_review_with_question, "# Clarified spec"
        return spec_review_no_questions, "# Clarified spec"

    with patch.object(agent, "_run_sop_phase1", return_value=([], "# Original spec", [])):
        with patch.object(
            agent, "_run_sop_phase2_architecture", return_value=(MagicMock(), "# Original spec")
        ):
            with patch.object(agent, "_run_spec_review", side_effect=run_spec_review):
                with patch.object(agent, "_communicate_with_user") as mock_comm:
                    mock_comm.return_value = [
                        AnsweredQuestion(
                            question_id="q1",
                            question_text="Which OAuth provider?",
                            selected_answer="GitHub",
                        )
                    ]
                    with patch.object(agent, "_run_spec_cleanup", return_value=cleanup_result):
                        with patch.object(agent, "_generate_prd_document", return_value="# PRD"):
                            result = agent.run_workflow(
                                spec_content="# Original spec",
                                repo_path=tmp_path,
                                job_id="test-job",
                                job_updater=lambda **kw: None,
                            )

    assert result.success
    assert len(run_spec_review_calls) == 2, (
        "Should call _run_spec_review twice (initial + re-run after clarification)"
    )
    assert run_spec_review_calls[0] == "# Original spec"
    assert run_spec_review_calls[1] == "# Clarified spec"
    assert any("Re-ran spec review on clarified spec" in rec.message for rec in caplog.records), (
        "Should log that spec review was re-run after clarification"
    )


def test_run_workflow_renames_validated_spec_when_needs_more_detail(tmp_path: Path) -> None:
    """When input is validated_spec.md and agent has open questions, rename it to updated_spec_v1 then write v2 for update."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    validated = tmp_path / "plan" / "product_analysis" / "validated_spec.md"
    validated.write_text("# Validated content")

    one_question = OpenQuestion(
        id="q1",
        question_text="Which framework?",
        options=[
            QuestionOption(id="opt1", label="React", is_default=True, rationale="", confidence=0.9)
        ],
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
            return spec_review_with_question, kwargs.get(
                "spec_content", args[0] if args else "# Validated content"
            )
        return spec_review_no_questions, "# Validated content\n# Updated"

    with patch.object(agent, "_communicate_with_user") as mock_comm:
        mock_comm.return_value = [
            AnsweredQuestion(
                question_id="q1", question_text="Which framework?", selected_answer="React"
            )
        ]
        with patch.object(agent, "_run_spec_review", side_effect=run_spec_review):
            with patch.object(
                agent, "_run_sop_phase1", return_value=([], "# Validated content", [])
            ):
                with patch.object(
                    agent,
                    "_run_sop_phase2_architecture",
                    return_value=(MagicMock(), "# Validated content"),
                ):
                    with patch.object(
                        agent,
                        "_run_spec_cleanup",
                        return_value=SpecCleanupResult(
                            is_valid=True,
                            validation_issues=[],
                            cleaned_spec="# Cleaned",
                            summary="Done",
                        ),
                    ):
                        with patch.object(agent, "_generate_prd_document", return_value="# PRD"):
                            result = agent.run_workflow(
                                spec_content="# Validated content",
                                repo_path=tmp_path,
                                job_id="test-job",
                                job_updater=lambda **kw: None,
                                initial_spec_path=validated,
                            )

    assert result.success
    v1 = tmp_path / "plan" / "product_analysis" / "updated_spec_v1.md"
    assert v1.exists(), (
        "validated_spec should have been renamed to updated_spec_v1.md (before final validated_spec write)"
    )
    assert v1.read_text() == "# Validated content", (
        "v1 should contain the original validated content from the rename"
    )
    assert len(update_spec_calls) >= 1
    assert update_spec_calls[0] == 2, "First _update_spec after rename should use version 2"


def test_run_workflow_writes_validated_spec_and_prd_separately(tmp_path: Path) -> None:
    """After a successful run, validated_spec.md contains the cleaned spec and product_requirements_document.md contains the PRD; they differ."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)

    cleaned_spec_content = "Cleaned normalized spec content."
    prd_content = "# Product Requirements Document\n\n## Executive Summary\n\nFull PRD with Open Questions section."

    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )
    cleanup_result = SpecCleanupResult(
        is_valid=True,
        validation_issues=[],
        cleaned_spec=cleaned_spec_content,
        summary="Cleaned",
    )

    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    with patch.object(agent, "_run_sop_phase1", return_value=([], "# Spec", [])):
        with patch.object(
            agent, "_run_sop_phase2_architecture", return_value=(MagicMock(), "# Spec")
        ):
            with patch.object(
                agent, "_run_spec_review", return_value=(spec_review_no_questions, "# Spec")
            ):
                with patch.object(agent, "_run_spec_cleanup", return_value=cleanup_result):
                    with patch.object(agent, "_generate_prd_document", return_value=prd_content):
                        result = agent.run_workflow(
                            spec_content="# Spec",
                            repo_path=tmp_path,
                            job_id="test-job",
                            job_updater=lambda **kw: None,
                        )

    assert result.success
    validated_path = tmp_path / "plan" / "product_analysis" / "validated_spec.md"
    prd_path = tmp_path / "plan" / "product_analysis" / "product_requirements_document.md"
    assert validated_path.exists(), "validated_spec.md should exist"
    assert prd_path.exists(), "product_requirements_document.md should exist"

    validated_text = validated_path.read_text()
    prd_text = prd_path.read_text()
    assert validated_text == cleaned_spec_content, (
        "validated_spec.md should contain the cleaned spec"
    )
    assert prd_text == prd_content, "product_requirements_document.md should contain the PRD"
    assert validated_text != prd_text, "validated spec and PRD must differ"
    assert "Executive Summary" in prd_text, "PRD should contain PRD template sections"
    assert "Executive Summary" not in validated_text, (
        "validated spec is cleaned spec, not the full PRD"
    )


def test_parse_open_question_preserves_extended_metadata() -> None:
    """_parse_open_question should keep constraint and lifecycle metadata fields."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-002",
            "question_text": "Which SLO tier should we target?",
            "context": "NFR targets are missing.",
            "category": "performance",
            "priority": "high",
            "constraint_domain": "backend",
            "constraint_layer": 3,
            "depends_on": "Q-001",
            "blocking": True,
            "owner": "user",
            "section_impact": ["Requirements", "Acceptance Criteria"],
            "due_date": "2026-03-06",
            "status": "asked",
            "asked_via": ["slack", "web_ui"],
            "options": [
                {
                    "id": "opt_standard",
                    "label": "Standard tier",
                    "is_default": True,
                    "rationale": "Balanced",
                    "confidence": 0.8,
                }
            ],
        },
        index=0,
    )

    assert parsed.constraint_domain == "backend"
    assert parsed.constraint_layer == 3
    assert parsed.depends_on == "Q-001"
    assert parsed.blocking is True
    assert parsed.owner == "user"
    assert parsed.section_impact == ["Requirements", "Acceptance Criteria"]
    assert parsed.due_date == "2026-03-06"
    assert parsed.status == "asked"
    assert parsed.asked_via == ["slack", "web_ui"]


def test_parse_open_question_wraps_non_sequence_section_impact_and_asked_via() -> None:
    """_parse_open_question should wrap a scalar string into a single-element list.

    A string is not iterated char-by-char (which would explode "Requirements" into
    ["R", "e", ...]); it is coerced to a single-element list, preserving the value
    rather than discarding it.
    """
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-004",
            "question_text": "Which SLO tier should we target?",
            "section_impact": "Requirements",
            "asked_via": "slack",
        },
        index=0,
    )

    assert parsed.section_impact == ["Requirements"]
    assert parsed.asked_via == ["slack"]


def test_parse_spec_review_response_coerces_malformed_question_without_raising() -> None:
    """parse_spec_review_response must never raise, even when an open question has non-list options.

    parse_open_question coerces a non-list 'options' value (e.g. explicit null) to an
    empty list rather than raising, so the malformed question is kept (with no options)
    instead of being dropped.
    """
    result = parse_spec_review_response(
        {
            "issues": ["Missing auth flow"],
            "gaps": ["No SLA defined"],
            "open_questions": [
                {"question_text": "Malformed question", "options": None},
                {"question_text": "Well-formed question", "options": []},
            ],
            "summary": "Reviewed",
        }
    )

    assert isinstance(result, SpecReviewResult)
    assert [q.question_text for q in result.open_questions] == [
        "Malformed question",
        "Well-formed question",
    ]
    assert result.open_questions[0].options == []


def test_parse_spec_review_response_does_not_cap_open_questions() -> None:
    """parse_spec_review_response keeps all parsed open questions uncapped.

    Cap is applied after organizational/duplicate filters so later material
    questions are not discarded when earlier entries are filtered out.
    """
    from product_requirements_analysis_agent.question_processing import MAX_OPEN_QUESTIONS

    questions = [
        {"id": f"q{i}", "question_text": f"Question {i}?", "options": []}
        for i in range(MAX_OPEN_QUESTIONS + 5)
    ]
    result = parse_spec_review_response(
        {
            "issues": [],
            "gaps": [],
            "open_questions": questions,
            "summary": "Reviewed",
        }
    )

    assert len(result.open_questions) == MAX_OPEN_QUESTIONS + 5
    assert result.open_questions[-1].id == f"q{MAX_OPEN_QUESTIONS + 4}"


def test_open_question_cap_applied_after_organizational_filter() -> None:
    """Organizational filtering must run before any open-question cap.

    Ten prohibited process questions followed by a material deployment question
    must leave the deployment question retained (not truncated before the filter).
    """
    from product_requirements_analysis_agent.question_processing import (
        MAX_OPEN_QUESTIONS,
        cap_open_questions,
        filter_organizational_questions,
    )

    org_questions = [
        OpenQuestion(
            id=f"org_{i}",
            question_text="Who has final decision / sign-off on this feature?",
            options=[],
        )
        for i in range(MAX_OPEN_QUESTIONS)
    ]
    material = OpenQuestion(
        id="deploy_l1",
        question_text="Where should the application be deployed?",
        options=[],
        category="infrastructure",
        priority="high",
    )
    parsed = parse_spec_review_response(
        {
            "issues": [],
            "gaps": [],
            "open_questions": [
                {
                    "id": q.id,
                    "question_text": q.question_text,
                    "options": [],
                    "category": q.category,
                    "priority": q.priority,
                }
                for q in [*org_questions, material]
            ],
            "summary": "Reviewed",
        }
    )
    assert len(parsed.open_questions) == MAX_OPEN_QUESTIONS + 1

    filtered = filter_organizational_questions(parsed.open_questions)
    # Cap is applied only after later consolidation/dedupe in the agent; this
    # asserts filter-before-cap ordering for retained material questions.
    capped = cap_open_questions(filtered)

    assert [q.id for q in capped] == ["deploy_l1"]


def test_cap_open_questions_preserves_order_and_limit() -> None:
    """cap_open_questions keeps the first N questions in order."""
    from product_requirements_analysis_agent.question_processing import (
        MAX_OPEN_QUESTIONS,
        cap_open_questions,
    )

    questions = [
        OpenQuestion(id=f"q{i}", question_text=f"Question {i}?", options=[])
        for i in range(MAX_OPEN_QUESTIONS + 3)
    ]
    capped = cap_open_questions(questions)
    assert [q.id for q in capped] == [f"q{i}" for i in range(MAX_OPEN_QUESTIONS)]
    assert cap_open_questions(questions[:3]) == questions[:3]


def test_parse_open_question_handles_non_numeric_constraint_layer() -> None:
    """_parse_open_question should fall back to 0 instead of raising on a non-numeric value."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-003",
            "question_text": "What is the constraint layer?",
            "constraint_layer": "high",
        },
        index=0,
    )

    assert parsed.constraint_layer == 0


def test_parse_open_question_preserves_option_order_when_marking_default() -> None:
    """_parse_open_question should keep the original option order, not sort by confidence."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-004",
            "question_text": "Which option should be default?",
            "options": [
                {"id": "opt_a", "label": "A", "is_default": False, "confidence": 0.3},
                {"id": "opt_b", "label": "B", "is_default": False, "confidence": 0.9},
                {"id": "opt_c", "label": "C", "is_default": False, "confidence": 0.5},
            ],
        },
        index=0,
    )

    assert [opt.id for opt in parsed.options] == ["opt_a", "opt_b", "opt_c"]
    assert [opt.is_default for opt in parsed.options] == [False, True, False]


def test_parse_open_question_treats_explicit_null_fields_as_empty() -> None:
    """_parse_open_question should not stringify explicit JSON null fields as the literal 'None'."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": None,
            "question_text": None,
            "context": None,
            "recommendation": None,
            "source": None,
            "category": None,
            "priority": None,
            "constraint_domain": None,
            "owner": None,
            "due_date": None,
            "status": None,
        },
        index=7,
    )

    assert parsed.id == "q7"
    assert parsed.question_text == ""
    assert parsed.context == ""
    assert parsed.recommendation == ""
    assert parsed.source == "spec_review"
    assert parsed.category == "general"
    assert parsed.priority == "medium"
    assert parsed.constraint_domain == ""
    assert parsed.owner == "user"
    assert parsed.due_date == ""
    assert parsed.status == "open"


def test_parse_open_question_treats_explicit_null_option_fields_as_empty() -> None:
    """Options with explicit null id/label/rationale should not become the literal 'None'."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "question_text": "Pick one",
            "options": [
                {
                    "id": None,
                    "label": None,
                    "rationale": None,
                    "is_default": True,
                    "confidence": 0.9,
                },
            ],
        },
        index=0,
    )

    assert parsed.options[0].id == "opt0"
    assert parsed.options[0].label == ""
    assert parsed.options[0].rationale == ""


def test_parse_question_option_defaults_confidence_when_non_numeric_string() -> None:
    """A malformed LLM confidence value (non-numeric string) should default to 0.5, not raise."""
    parsed = parse_question_option({"id": "opt1", "label": "Yes", "confidence": "high"}, index=1)

    assert parsed.confidence == 0.5


def test_parse_question_option_defaults_confidence_when_none() -> None:
    """An explicit JSON null confidence value should default to 0.5, not raise."""
    parsed = parse_question_option({"id": "opt1", "label": "Yes", "confidence": None}, index=1)

    assert parsed.confidence == 0.5


def test_parse_question_option_preserves_valid_numeric_confidence() -> None:
    """Existing valid-numeric-confidence behavior is unchanged."""
    parsed = parse_question_option({"id": "opt1", "label": "Yes", "confidence": 0.9}, index=1)

    assert parsed.confidence == 0.9


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (True, False, True),
        (False, True, False),
        ("true", False, True),
        ("True", False, True),
        (" TRUE ", False, True),
        ("false", True, False),
        ("False", True, False),
        (" FALSE ", True, False),
        (None, False, False),
        (None, True, True),
        ("yes", False, False),
        ("yes", True, True),
        (1, False, False),
        (0, True, True),
    ],
)
def test_safe_bool(value: Any, default: bool, expected: bool) -> None:
    """_safe_bool must not use Python truthiness: bool('false') is True, which
    would silently invert an LLM-stringified boolean. A real bool passes
    through; a case-insensitive 'true'/'false' string (whitespace-stripped)
    resolves accordingly; anything else (including a non-boolean truthy value
    like 1) falls back to ``default``."""
    assert _safe_bool(value, default) is expected


def test_parse_question_option_treats_stringified_false_as_false() -> None:
    """A stringified 'false' for is_default must not be treated as truthy
    (bool('false') is True, which would incorrectly mark this option default)."""
    parsed = parse_question_option({"id": "opt1", "label": "Yes", "is_default": "false"}, index=0)

    assert parsed.is_default is False


def test_parse_open_question_treats_stringified_false_as_false_for_allow_multiple_and_blocking() -> (
    None
):
    """A stringified 'false' for allow_multiple/blocking must not be treated as truthy."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "q1",
            "question_text": "Which region?",
            "allow_multiple": "false",
            "blocking": "false",
        },
        index=0,
    )

    assert parsed.allow_multiple is False
    assert parsed.blocking is False


@pytest.mark.parametrize(
    "malformed_confidence", [None, "high", float("nan"), -3, 7, 10**400]
)
def test_parse_question_option_clamps_or_defaults_confidence(
    malformed_confidence: Any,
) -> None:
    """confidence should always end up in [0.0, 1.0], including out-of-range/overflow values."""
    parsed = parse_question_option(
        {"id": "opt1", "label": "Yes", "confidence": malformed_confidence}, index=1
    )

    assert 0.0 <= parsed.confidence <= 1.0


@pytest.mark.parametrize(
    ("field", "malformed_value", "expected"),
    [
        ("section_impact", None, []),
        ("section_impact", 5, ["5"]),
        ("section_impact", "Requirements", ["Requirements"]),
        ("asked_via", None, []),
        ("asked_via", 5, ["5"]),
        ("asked_via", "slack", ["slack"]),
    ],
)
def test_parse_open_question_coerces_non_list_fields(
    field: str, malformed_value: Any, expected: list
) -> None:
    """_parse_open_question should coerce non-list LLM output instead of raising."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-011",
            "question_text": "Which region should we deploy to?",
            field: malformed_value,
        },
        index=0,
    )

    assert getattr(parsed, field) == expected


@pytest.mark.parametrize(
    ("malformed_value", "expected_count", "expected_label"),
    [
        (None, 0, None),
        (5, 1, "5"),
        ("us-east", 1, "us-east"),
    ],
)
def test_parse_open_question_coerces_non_list_options(
    malformed_value: Any, expected_count: int, expected_label: str
) -> None:
    """_parse_open_question should coerce a non-list 'options' field instead of raising."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-012",
            "question_text": "Which region should we deploy to?",
            "options": malformed_value,
        },
        index=0,
    )

    assert len(parsed.options) == expected_count
    if expected_label is not None:
        assert parsed.options[0].label == expected_label


@pytest.mark.parametrize("malformed_confidence", [None, "high", float("nan"), -3, 7, 10**400])
def test_parse_open_question_coerces_scalar_option_with_malformed_confidence(
    malformed_confidence: Any,
) -> None:
    """A single option object with a malformed confidence should not raise.

    Wrapping a scalar 'options' dict into a one-item list (via _coerce_list) now
    routes it through parse_question_option's dict branch, which previously
    assumed 'confidence' was always a valid float in [0.0, 1.0].
    """
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    parsed = agent._parse_open_question(
        {
            "id": "Q-013",
            "question_text": "Which region should we deploy to?",
            "options": {
                "id": "opt_a",
                "label": "us-east",
                "confidence": malformed_confidence,
            },
        },
        index=0,
    )

    assert len(parsed.options) == 1
    assert 0.0 <= parsed.options[0].confidence <= 1.0


def test_convert_to_pending_questions_includes_extended_metadata() -> None:
    """Pending question payload should include gate-aware metadata for UI and orchestration."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    open_questions = [
        OpenQuestion(
            id="Q-100",
            question_text="Choose deployment option",
            context="Infrastructure unresolved",
            category="infrastructure",
            priority="high",
            constraint_domain="infrastructure",
            constraint_layer=1,
            depends_on=None,
            blocking=True,
            owner="stakeholder",
            section_impact=["Technical Approach"],
            due_date="2026-03-10",
            status="open",
            asked_via=["email"],
            options=[
                QuestionOption(
                    id="opt_paas", label="PaaS", is_default=True, rationale="", confidence=0.7
                )
            ],
        )
    ]

    pending = agent._convert_to_pending_questions(open_questions)

    assert pending[0]["constraint_domain"] == "infrastructure"
    assert pending[0]["constraint_layer"] == 1
    assert pending[0]["blocking"] is True
    assert pending[0]["owner"] == "stakeholder"
    assert pending[0]["section_impact"] == ["Technical Approach"]
    assert pending[0]["due_date"] == "2026-03-10"
    assert pending[0]["status"] == "open"
    assert pending[0]["asked_via"] == ["email"]


def test_convert_to_pending_questions_appends_recommendation_when_set() -> None:
    """When OpenQuestion has recommendation set, pending context should include 'Recommendation: ...'."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    open_questions = [
        OpenQuestion(
            id="Q-1",
            question_text="Which auth?",
            context="Spec does not specify auth.",
            recommendation="We recommend OAuth with a single provider for the MVP.",
            options=[
                QuestionOption(
                    id="opt_oauth", label="OAuth", is_default=True, rationale="", confidence=0.8
                )
            ],
        )
    ]
    pending = agent._convert_to_pending_questions(open_questions)
    assert pending[0]["recommendation"] is not None
    assert "We recommend OAuth with a single provider" in pending[0]["recommendation"]


def test_review_question_answer_alignment_returns_empty_when_no_questions() -> None:
    """_review_question_answer_alignment should return [] when given empty list (no LLM call)."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._review_question_answer_alignment([])
    assert result == []
    llm.complete_json.assert_not_called()


def test_add_recommendations_returns_unchanged_when_no_questions() -> None:
    """_add_recommendations should return the same list when given empty list (no LLM call)."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._add_recommendations([], "# Spec content")
    assert result == []


def test_add_recommendations_treats_null_recommendation_as_absent() -> None:
    """A null ``recommendation`` from the LLM must leave the question's recommendation
    empty, not the literal string "None" (str(None) coerced from an unconditional
    str() call on the raw value)."""
    llm = _StubClient(
        {
            "recommendations": [
                {"id": "q1", "recommendation": None},
                {"id": "q2", "recommendation": "Use OAuth for the MVP."},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Which auth?")
    q2 = OpenQuestion(id="q2", question_text="Which storage?")

    result = agent._add_recommendations([q1, q2], "# Spec content")

    result_by_id = {q.id: q for q in result}
    assert result_by_id["q1"].recommendation == ""
    assert result_by_id["q2"].recommendation == "Use OAuth for the MVP."


def test_build_specialist_collaboration_plan_recommends_ui_arch_and_risk_agents() -> None:
    """Specialist plan should include new UI/UX, architecture, and risk-focused agents when relevant."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)

    cleaned_spec = """
    Build a web UI onboarding workflow with multiple screens and design consistency.
    The architecture includes API integrations, event tracking dashboards, and phased rollout.
    We must capture key risks, dependencies, and security/privacy requirements.
    """

    plan = agent._build_specialist_collaboration_plan(cleaned_spec, answered_questions=[])

    assert "UX and Flows Agent" in plan
    assert "Design System Tool Agent" in plan
    assert "Branding Guidance Agent" in plan
    assert "Architecture Agent" in plan
    assert "API and Integration Agent" in plan
    assert "Risk Analysis Agent" in plan
    assert "Security, Privacy, and Compliance Agent" in plan
    assert "Data and Analytics Agent" in plan


def test_consolidate_open_questions_parses_llm_output_into_open_questions() -> None:
    """_consolidate_open_questions should parse valid LLM JSON into List[OpenQuestion] with expected shape."""
    llm = _StubClient(
        {
            "consolidated_questions": [
                {
                    "id": "auth_approach",
                    "question_text": "Which authentication approach do you want?",
                    "context": "Spec does not specify auth.",
                    "category": "security",
                    "priority": "high",
                    "allow_multiple": False,
                    "constraint_domain": "auth",
                    "constraint_layer": 2,
                    "depends_on": None,
                    "blocking": True,
                    "owner": "user",
                    "section_impact": ["Technical Approach"],
                    "due_date": "2026-03-06",
                    "status": "open",
                    "asked_via": ["web_ui"],
                    "options": [
                        {
                            "id": "opt_oauth",
                            "label": "OAuth (e.g. Google)",
                            "is_default": True,
                            "rationale": "Simple",
                            "confidence": 0.8,
                        },
                        {
                            "id": "opt_sso",
                            "label": "Enterprise SSO",
                            "is_default": False,
                            "rationale": "Enterprise",
                            "confidence": 0.5,
                        },
                    ],
                }
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(
        id="q1",
        question_text="Do you want Google only for OAuth?",
        options=[
            QuestionOption(id="o1", label="Yes", is_default=True, rationale="", confidence=0.5)
        ],
    )
    q2 = OpenQuestion(
        id="q2",
        question_text="What is the right provider? OAuth or Enterprise?",
        options=[
            QuestionOption(id="o2", label="OAuth", is_default=True, rationale="", confidence=0.5)
        ],
    )
    result = agent._consolidate_open_questions([q1, q2])
    assert len(result) == 1
    assert result[0].id == "auth_approach"
    assert "authentication approach" in result[0].question_text
    assert len(result[0].options) == 2
    assert result[0].options[0].id == "opt_oauth"
    assert result[0].options[1].id == "opt_sso"


def test_consolidate_open_questions_sends_full_orchestration_metadata_to_llm() -> None:
    """The consolidation prompt must include orchestration metadata (id, constraint_domain,
    constraint_layer, depends_on, blocking, owner, section_impact, due_date, status,
    asked_via), matching what CONSOLIDATE_QUESTIONS_PROMPT instructs the LLM to preserve.
    Otherwise the LLM never sees these fields and can't echo them back, and
    parse_open_question silently resets them to defaults."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(
        id="q1",
        question_text="Which region?",
        constraint_domain="infrastructure",
        constraint_layer=2,
        depends_on="q0",
        blocking=False,
        owner="stakeholder",
        section_impact=["Technical Approach"],
        due_date="2026-04-01",
        status="asked",
        asked_via=["slack"],
        options=[
            QuestionOption(id="o1", label="us-east", is_default=True, rationale="", confidence=0.5)
        ],
    )
    q2 = OpenQuestion(
        id="q2",
        question_text="Which zone?",
        options=[
            QuestionOption(id="o2", label="zone-a", is_default=True, rationale="", confidence=0.5)
        ],
    )

    with patch(
        "product_requirements_analysis_agent.question_processing.call_llm_json"
    ) as mock_call:
        mock_call.return_value = {"consolidated_questions": []}
        agent._consolidate_open_questions([q1, q2])

    sent_prompt = mock_call.call_args[0][1]
    for field in [
        '"id": "q1"',
        '"constraint_domain": "infrastructure"',
        '"constraint_layer": 2',
        '"depends_on": "q0"',
        '"blocking": false',
        '"owner": "stakeholder"',
        '"section_impact"',
        '"due_date": "2026-04-01"',
        '"status": "asked"',
        '"asked_via"',
    ]:
        assert field in sent_prompt, f"expected {field!r} in consolidation prompt"


def test_consolidate_open_questions_does_not_raise_on_non_serializable_due_date() -> None:
    """The prompt-building json.dumps must not raise TypeError even if due_date somehow
    ends up holding a non-JSON-native value (e.g. via model_copy bypassing validation) —
    the docstring promises this function never raises."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Which region?").model_copy(
        update={"due_date": date(2026, 4, 1)}
    )
    q2 = OpenQuestion(id="q2", question_text="Which zone?")

    with patch(
        "product_requirements_analysis_agent.question_processing.call_llm_json"
    ) as mock_call:
        mock_call.return_value = {"consolidated_questions": []}
        agent._consolidate_open_questions([q1, q2])

    sent_prompt = mock_call.call_args[0][1]
    assert "2026-04-01" in sent_prompt


def test_review_question_answer_alignment_parses_llm_output_and_preserves_ids() -> None:
    """_review_question_answer_alignment should return List[OpenQuestion] with same ids when LLM returns valid aligned_questions."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {
                    "id": "infra_q",
                    "question_text": "What platform category for deployment?",
                    "context": "Spec does not specify.",
                    "category": "infrastructure",
                    "priority": "high",
                    "allow_multiple": False,
                    "constraint_domain": "infrastructure",
                    "constraint_layer": 1,
                    "depends_on": None,
                    "blocking": True,
                    "owner": "user",
                    "section_impact": [],
                    "due_date": "",
                    "status": "open",
                    "asked_via": ["web_ui"],
                    "options": [
                        {
                            "id": "opt_paas",
                            "label": "PaaS (Heroku, Render)",
                            "is_default": True,
                            "rationale": "",
                            "confidence": 0.7,
                        },
                    ],
                }
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q = OpenQuestion(
        id="infra_q",
        question_text="What platform category for deployment?",
        options=[
            QuestionOption(
                id="opt_paas", label="PaaS", is_default=True, rationale="", confidence=0.7
            )
        ],
    )
    result = agent._review_question_answer_alignment([q])
    assert len(result) == 1
    assert result[0].id == "infra_q"
    assert result[0].question_text == "What platform category for deployment?"


def test_review_question_answer_alignment_does_not_raise_on_non_serializable_due_date() -> None:
    """The prompt-building json.dumps must not raise TypeError even if due_date somehow
    ends up holding a non-JSON-native value (e.g. via model_copy bypassing validation) —
    the docstring promises this function never raises."""
    llm = _StubClient({"aligned_questions": []})
    agent = ProductRequirementsAnalysisAgent(llm)
    q = OpenQuestion(id="q1", question_text="Which region?").model_copy(
        update={"due_date": date(2026, 4, 1)}
    )

    result = agent._review_question_answer_alignment([q])

    assert len(result) == 1
    assert result[0].id == "q1"


def test_consolidate_open_questions_skips_malformed_item_keeps_valid_ones() -> None:
    """A single unparseable item should be skipped, not discard the whole consolidated batch."""
    llm = _StubClient(
        {
            "consolidated_questions": [
                {"id": "bad", "question_text": "malformed"},
                {"id": "good", "question_text": "Which platform?"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    real_parse = _real_parse_open_question

    def flaky_parse(q_data: Any, index: int) -> OpenQuestion:
        if q_data.get("id") == "bad":
            raise ValueError("simulated malformed item")
        return real_parse(q_data, index)

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=flaky_parse,
    ):
        result = agent._consolidate_open_questions([q1, q2])

    assert len(result) == 1
    assert result[0].id == "good"


def test_consolidate_open_questions_falls_back_when_all_items_fail() -> None:
    """If every item in the batch fails to parse, the original list is returned unchanged."""
    llm = _StubClient(
        {
            "consolidated_questions": [
                {"id": "bad1", "question_text": "malformed"},
                {"id": "bad2", "question_text": "also malformed"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=ValueError("simulated malformed item"),
    ):
        result = agent._consolidate_open_questions([q1, q2])

    assert result == [q1, q2]


def test_review_question_answer_alignment_retains_original_when_item_fails_to_parse() -> None:
    """A single unparseable aligned item should fall back to its original question by id,
    not be dropped from the batch (alignment is per-question; ids are preserved)."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "q1", "question_text": "malformed"},
                {"id": "q2", "question_text": "Which platform?"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    real_parse = _real_parse_open_question

    def flaky_parse(q_data: Any, index: int) -> OpenQuestion:
        if q_data.get("id") == "q1":
            raise ValueError("simulated malformed item")
        return real_parse(q_data, index)

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=flaky_parse,
    ):
        result = agent._review_question_answer_alignment([q1, q2])

    assert len(result) == 2
    result_by_id = {q.id: q for q in result}
    assert result_by_id["q1"] is q1
    assert result_by_id["q2"].question_text == "Which platform?"


def test_review_question_answer_alignment_restores_original_for_unmatched_malformed_item() -> None:
    """A malformed aligned item whose id does not match any original question is dropped;
    the original question list is preserved unchanged."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "unknown", "question_text": "malformed"},
                {"id": "q2", "question_text": "Which platform?"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    real_parse = _real_parse_open_question

    def flaky_parse(q_data: Any, index: int) -> OpenQuestion:
        if q_data.get("id") == "unknown":
            raise ValueError("simulated malformed item")
        return real_parse(q_data, index)

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=flaky_parse,
    ):
        result = agent._review_question_answer_alignment([q1, q2])

    assert len(result) == 2
    result_by_id = {q.id: q for q in result}
    assert result_by_id["q1"] is q1
    assert result_by_id["q2"].question_text == "Which platform?"


def test_review_question_answer_alignment_restores_original_when_two_items_fail_one_unmatched() -> (
    None
):
    """Regression for dropping an original question when one failed item matches by id and
    another failed item has an unrecognized id: both originals must survive, not just the
    id-matched fallback."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "q1", "question_text": "malformed"},
                {"id": "unknown", "question_text": "also malformed"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=ValueError("simulated malformed item"),
    ):
        result = agent._review_question_answer_alignment([q1, q2])

    assert len(result) == 2
    result_by_id = {q.id: q for q in result}
    assert result_by_id["q1"] is q1
    assert result_by_id["q2"] is q2


def test_review_question_answer_alignment_rejects_hallucinated_id() -> None:
    """An aligned item that parses successfully but carries an id absent from the original
    questions (a hallucinated id) must not be added to the result; the original question it
    displaced is restored instead, and the batch is never inflated beyond the originals."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "unknown", "question_text": "A question that was never asked"},
                {"id": "q2", "question_text": "Which platform?"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    result = agent._review_question_answer_alignment([q1, q2])

    assert len(result) == 2
    result_by_id = {q.id: q for q in result}
    assert set(result_by_id) == {"q1", "q2"}
    assert result_by_id["q1"] is q1
    assert result_by_id["q2"].question_text == "Which platform?"


def test_review_question_answer_alignment_rejects_duplicate_id() -> None:
    """If the LLM returns the same recognized id twice, only the first occurrence is kept;
    the repeat is dropped rather than duplicating the question, and any omitted original
    (here q2) is still restored."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "q1", "question_text": "Aligned once"},
                {"id": "q1", "question_text": "Aligned again"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    result = agent._review_question_answer_alignment([q1, q2])

    assert len(result) == 2
    result_by_id = {q.id: q for q in result}
    assert set(result_by_id) == {"q1", "q2"}
    assert result_by_id["q1"].question_text == "Aligned once"
    assert result_by_id["q2"] is q2
    assert len([q for q in result if q.id == "q1"]) == 1


def test_review_question_answer_alignment_falls_back_when_all_items_fail() -> None:
    """If every item in the batch fails to parse, the original list is returned unchanged."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "bad1", "question_text": "malformed"},
                {"id": "bad2", "question_text": "also malformed"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=ValueError("simulated malformed item"),
    ):
        result = agent._review_question_answer_alignment([q1, q2])

    assert result == [q1, q2]


def test_review_question_answer_alignment_preserves_input_order_when_all_items_fail_but_ids_match() -> (
    None
):
    """If every item fails to parse but each still carries a recognizable original id, the
    id-matched fallbacks must not be returned in the LLM's (possibly reordered) order —
    since nothing was genuinely realigned, the original input order is preserved instead."""
    llm = _StubClient(
        {
            "aligned_questions": [
                {"id": "q2", "question_text": "malformed"},
                {"id": "q1", "question_text": "also malformed"},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(id="q1", question_text="Original question 1?")
    q2 = OpenQuestion(id="q2", question_text="Original question 2?")

    with patch(
        "product_requirements_analysis_agent.question_processing.parse_open_question",
        side_effect=ValueError("simulated malformed item"),
    ):
        result = agent._review_question_answer_alignment([q1, q2])

    assert result == [q1, q2]


def test_dedupe_questions_by_answer_similarity_drops_question_when_we_already_have_that_answer() -> (
    None
):
    """_dedupe_questions_by_answer_similarity drops open questions whose option matches an existing answer."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    answered = [
        AnsweredQuestion(
            question_id="prev",
            question_text="Where to deploy?",
            selected_answer="PaaS",
        )
    ]
    opt_paas = QuestionOption(id="o1", label="PaaS", is_default=True, rationale="", confidence=0.9)
    opt_k8s = QuestionOption(
        id="o2", label="Kubernetes", is_default=False, rationale="", confidence=0.5
    )
    q_already_answered = OpenQuestion(
        id="a",
        question_text="Which deployment target?",
        options=[opt_paas, opt_k8s],
    )
    q_new = OpenQuestion(
        id="b",
        question_text="Which OAuth provider?",
        options=[
            QuestionOption(id="o3", label="GitHub", is_default=True, rationale="", confidence=0.8),
            QuestionOption(id="o4", label="Google", is_default=False, rationale="", confidence=0.5),
        ],
    )
    result = agent._dedupe_questions_by_answer_similarity(
        [q_already_answered, q_new],
        answered,
    )
    assert len(result) == 1
    assert result[0].id == "b"
    assert result[0].question_text == "Which OAuth provider?"


def test_dedupe_questions_by_answer_similarity_keeps_all_when_no_answers() -> None:
    """When there are no answered questions, all open questions are kept."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    opt = QuestionOption(id="o1", label="Yes", is_default=True, rationale="", confidence=0.9)
    q1 = OpenQuestion(id="a", question_text="Which OAuth provider?", options=[opt])
    q2 = OpenQuestion(id="b", question_text="Where to deploy?", options=[opt])
    result = agent._dedupe_questions_by_answer_similarity([q1, q2], [])
    assert len(result) == 2
    assert result[0].id == "a"
    assert result[1].id == "b"


def test_dedupe_questions_by_answer_similarity_keeps_questions_with_no_options() -> None:
    """Open questions with no options are kept (we cannot infer answer overlap)."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    answered = [
        AnsweredQuestion(question_id="x", question_text="Something?", selected_answer="Yes"),
    ]
    q_no_opts = OpenQuestion(id="n", question_text="Free-form question?", options=[])
    result = agent._dedupe_questions_by_answer_similarity([q_no_opts], answered)
    assert len(result) == 1
    assert result[0].id == "n"


def test_dedupe_questions_by_answer_similarity_dedupes_repeated_existing_answers() -> None:
    """Repeated identical selected_answer/other_text values across answered_questions
    should not change the outcome (existing_answers is a set, so duplicates collapse
    instead of being stored/compared repeatedly)."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    answered = [
        AnsweredQuestion(question_id="x1", question_text="Where?", selected_answer="PaaS"),
        AnsweredQuestion(question_id="x2", question_text="Where else?", selected_answer="PaaS"),
        AnsweredQuestion(
            question_id="x3",
            question_text="Anything custom?",
            selected_answer="Other",
            other_text="Managed hosting",
        ),
        AnsweredQuestion(
            question_id="x4",
            question_text="Anything else custom?",
            selected_answer="Other",
            other_text="Managed hosting",
        ),
    ]
    q_paas = OpenQuestion(
        id="a",
        question_text="Which deployment target?",
        options=[QuestionOption(id="o1", label="PaaS", is_default=True, rationale="", confidence=0.9)],
    )
    q_hosting = OpenQuestion(
        id="b",
        question_text="Which hosting approach?",
        options=[
            QuestionOption(
                id="o2", label="Managed hosting", is_default=True, rationale="", confidence=0.9
            )
        ],
    )
    result = agent._dedupe_questions_by_answer_similarity([q_paas, q_hosting], answered)
    assert result == []


def test_filter_organizational_questions_removes_org_keeps_technical() -> None:
    """_filter_organizational_questions removes organizational/process questions and keeps technical ones."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    opt = QuestionOption(id="o1", label="Option", is_default=True, rationale="", confidence=0.8)
    q_org = OpenQuestion(
        id="org1",
        question_text="What is the approval process for this feature?",
        options=[opt],
    )
    q_tech = OpenQuestion(
        id="tech1",
        question_text="Which OAuth provider?",
        options=[opt],
    )
    result = agent._filter_organizational_questions([q_org, q_tech])
    assert len(result) == 1
    assert result[0].id == "tech1"
    assert result[0].question_text == "Which OAuth provider?"


def test_record_answers_supersede_removes_old_qa_from_history(tmp_path: Path) -> None:
    """When a new answer is the same decision as an existing Q&A, the old entry is removed and the new one is recorded."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    qa_file = tmp_path / "plan" / "product_analysis" / "qa_history.md"
    qa_file.write_text(
        "# Q&A History\n\n"
        "This file records all questions and answers from Product Requirements Analysis.\n"
        "\n## Iteration 1\n\n"
        "### Which OAuth provider?\n"
        "**Answer:** GitHub\n\n"
        "\n## Iteration 2\n\n"
        "### Use SAML for SSO?\n"
        "**Answer:** SAML\n\n",
        encoding="utf-8",
    )
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    # New answer that supersedes the first (same decision: OAuth / auth method)
    new_answer = AnsweredQuestion(
        question_id="q1",
        question_text="Which OAuth provider for the MVP?",
        selected_answer="SAML",
    )
    agent._record_answers(tmp_path, [new_answer], iteration=3)
    content = qa_file.read_text(encoding="utf-8")
    assert "**Answer:** GitHub" not in content
    assert "Which OAuth provider for the MVP?" in content
    assert "**Answer:** SAML" in content
    assert "Iteration 3" in content


def test_record_answers_different_topic_keeps_existing_qa(tmp_path: Path) -> None:
    """When the new answer is unrelated, existing Q&A is kept and the new one is appended."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    qa_file = tmp_path / "plan" / "product_analysis" / "qa_history.md"
    qa_file.write_text(
        "# Q&A History\n\n"
        "This file records all questions and answers from Product Requirements Analysis.\n"
        "\n## Iteration 1\n\n"
        "### Which OAuth provider?\n"
        "**Answer:** GitHub\n\n",
        encoding="utf-8",
    )
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    new_answer = AnsweredQuestion(
        question_id="q2",
        question_text="Where to deploy?",
        selected_answer="AWS",
    )
    agent._record_answers(tmp_path, [new_answer], iteration=2)
    content = qa_file.read_text(encoding="utf-8")
    assert "Which OAuth provider?" in content
    assert "**Answer:** GitHub" in content
    assert "Where to deploy?" in content
    assert "**Answer:** AWS" in content
    assert "Iteration 2" in content


# ---------------------------------------------------------------------------
# Context and constraints discovery (pre-review)
# ---------------------------------------------------------------------------


def test_run_context_constraints_discovery_returns_questions_when_llm_valid() -> None:
    """_run_context_constraints_discovery returns non-empty List[OpenQuestion] when LLM returns valid JSON."""
    llm = MagicMock()
    llm.complete_text.return_value = """{
      "open_questions": [
        {
          "id": "ctx_project_type",
          "question_text": "What type of organization is this?",
          "context": "Shapes MVP scope.",
          "category": "business",
          "priority": "high",
          "allow_multiple": false,
          "constraint_domain": "",
          "constraint_layer": 0,
          "options": [
            {"id": "opt_startup", "label": "Startup", "is_default": true, "rationale": "", "confidence": 0.7},
            {"id": "opt_enterprise", "label": "Enterprise", "is_default": false, "rationale": "", "confidence": 0.5}
          ]
        }
      ]
    }"""
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._run_context_constraints_discovery("# Spec")
    assert len(result) >= 1
    assert result[0].id == "ctx_project_type"
    assert "organization" in result[0].question_text
    assert result[0].source == "context_discovery"
    assert len(result[0].options) == 2


def test_run_context_constraints_discovery_uses_fallback_on_llm_failure() -> None:
    """_run_context_constraints_discovery uses fixed fallback when LLM raises or returns empty/invalid."""
    llm = MagicMock()
    llm.complete_text.side_effect = Exception("LLM unavailable")
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._run_context_constraints_discovery("# Spec")
    fallback = context_discovery_fallback_questions()
    assert len(result) == len(fallback)
    assert all(q.source == "context_discovery" for q in result)
    ids = [q.id for q in result]
    assert "ctx_project_type" in ids
    assert "ctx_deployment" in ids
    assert "ctx_sla" in ids


def test_run_context_constraints_discovery_uses_fallback_on_empty_json() -> None:
    """_run_context_constraints_discovery uses fallback when LLM returns empty open_questions."""
    llm = MagicMock()
    llm.complete_text.return_value = '{"open_questions": []}'
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._run_context_constraints_discovery("# Spec")
    fallback = context_discovery_fallback_questions()
    assert len(result) == len(fallback)


def test_inject_context_answers_into_spec_prepends_section() -> None:
    """_inject_context_answers_into_spec returns spec starting with '## Project context and constraints' and containing Q&A."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    answered = [
        AnsweredQuestion(
            question_id="ctx_1",
            question_text="What type of organization?",
            selected_answer="Startup",
        ),
        AnsweredQuestion(
            question_id="ctx_2",
            question_text="Where to deploy?",
            selected_answer="Cloud",
        ),
    ]
    current_spec = "# Original spec\n\nSome content."
    result = agent._inject_context_answers_into_spec(current_spec, answered)
    assert result.startswith("## Project context and constraints")
    assert "What type of organization?" in result
    assert "Startup" in result
    assert "Where to deploy?" in result
    assert "Cloud" in result
    assert "# Original spec" in result
    assert "Some content." in result


def test_run_workflow_skips_context_discovery_when_no_job_id(tmp_path: Path) -> None:
    """run_workflow with job_id=None does not call _run_sop_phase1; proceeds to spec review."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )
    cleanup_result = SpecCleanupResult(
        is_valid=True, validation_issues=[], cleaned_spec="# Cleaned", summary="Done"
    )
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    with patch.object(agent, "_run_sop_phase1") as mock_sop:
        with patch.object(
            agent, "_run_spec_review", return_value=(spec_review_no_questions, "# Spec")
        ):
            with patch.object(agent, "_run_spec_cleanup", return_value=cleanup_result):
                with patch.object(agent, "_generate_prd_document", return_value="# PRD"):
                    agent.run_workflow(
                        spec_content="# Spec",
                        repo_path=tmp_path,
                        job_id=None,
                        job_updater=lambda **kw: None,
                    )
    mock_sop.assert_not_called()


def test_run_workflow_with_sop_phase1_injects_into_spec(tmp_path: Path) -> None:
    """With job_id set, SOP Phase 1 runs; first spec review receives spec that includes injected context section."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    sop_answered = [
        AnsweredQuestion(
            question_id="P1.deploy.a",
            question_text="Where to deploy?",
            selected_answer="Cloud",
        )
    ]
    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )
    cleanup_result = SpecCleanupResult(
        is_valid=True, validation_issues=[], cleaned_spec="# Cleaned", summary="Done"
    )
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    spec_review_received_specs = []

    def capture_spec_review(*args, **kwargs):
        spec = args[0] if args else kwargs.get("spec_content", "")
        spec_review_received_specs.append(spec)
        return spec_review_no_questions, spec

    injected_spec = (
        "## Project context and constraints\n\nQ: Where to deploy?\nA: Cloud\n\n---\n\n# Original"
    )
    with patch.object(agent, "_run_sop_phase1", return_value=([], injected_spec, sop_answered)):
        with patch.object(
            agent, "_run_sop_phase2_architecture", return_value=(MagicMock(), injected_spec)
        ):
            with patch.object(agent, "_run_spec_review", side_effect=capture_spec_review):
                with patch.object(agent, "_run_spec_cleanup", return_value=cleanup_result):
                    with patch.object(agent, "_generate_prd_document", return_value="# PRD"):
                        result = agent.run_workflow(
                            spec_content="# Original",
                            repo_path=tmp_path,
                            job_id="test-job",
                            job_updater=lambda **kw: None,
                        )
    assert result.success
    assert len(spec_review_received_specs) >= 1
    first_spec = spec_review_received_specs[0]
    assert "Project context and constraints" in first_spec
    assert "Where to deploy?" in first_spec
    assert "Cloud" in first_spec


# ---------------------------------------------------------------------------
# SOP Phase 1 & 2 Tests
# ---------------------------------------------------------------------------


def test_sop_phase1_questions_registry_complete() -> None:
    """All 10 SOPSubPhase values have entries in SOP_PHASE1_QUESTIONS."""
    for sub_phase in SOPSubPhase:
        assert sub_phase in SOP_PHASE1_QUESTIONS, f"Missing registry entry for {sub_phase.value}"
        assert len(SOP_PHASE1_QUESTIONS[sub_phase]) > 0, (
            f"Empty question list for {sub_phase.value}"
        )


def test_sop_phase1_questions_unique_ids() -> None:
    """All SOP question IDs are unique across all sub-phases."""
    all_ids = []
    for q_defs in SOP_PHASE1_QUESTIONS.values():
        for q_def in q_defs:
            all_ids.append(q_def["sop_id"])
    assert len(all_ids) == len(set(all_ids)), (
        f"Duplicate SOP IDs found: {[x for x in all_ids if all_ids.count(x) > 1]}"
    )


def test_sop_phase1_fallback_questions() -> None:
    """Fallback covers all 10 sub-phases and skips conditional questions."""
    fallback = _sop_phase1_fallback_questions()
    assert len(fallback) > 0

    # All root sub-phases should be represented
    sub_phases_covered = {q.sop_sub_phase for q in fallback}
    for sub_phase in SOPSubPhase:
        assert sub_phase.value in sub_phases_covered, (
            f"Fallback missing sub-phase: {sub_phase.value}"
        )

    # No conditional questions (depends_on != None) should be in fallback
    conditional_ids = set()
    for q_defs in SOP_PHASE1_QUESTIONS.values():
        for q_def in q_defs:
            if q_def.get("depends_on") is not None:
                conditional_ids.add(q_def["sop_id"])
    for q in fallback:
        assert q.id not in conditional_ids, f"Conditional question {q.id} should not be in fallback"


def test_evaluate_sop_conditionals_no_depends() -> None:
    """Questions without depends_on should always be asked."""
    q_def = {"sop_id": "P1.deploy.a", "depends_on": None}
    result = ProductRequirementsAnalysisAgent._evaluate_sop_conditionals(q_def, {})
    assert result is True


def test_evaluate_sop_conditionals_parent_not_answered() -> None:
    """Questions whose parent isn't answered yet should be deferred (None)."""
    q_def = {"sop_id": "P1.deploy.b", "depends_on": {"P1.deploy.a": ["Cloud"]}}
    result = ProductRequirementsAnalysisAgent._evaluate_sop_conditionals(q_def, {})
    assert result is None


def test_evaluate_sop_conditionals_condition_met() -> None:
    """Questions whose parent answer matches should be asked."""
    q_def = {"sop_id": "P1.deploy.b", "depends_on": {"P1.deploy.a": ["Cloud", "Hybrid"]}}
    result = ProductRequirementsAnalysisAgent._evaluate_sop_conditionals(
        q_def, {"P1.deploy.a": "Cloud"}
    )
    assert result is True


def test_evaluate_sop_conditionals_condition_not_met() -> None:
    """Questions whose parent answer doesn't match should be skipped."""
    q_def = {"sop_id": "P1.deploy.b", "depends_on": {"P1.deploy.a": ["Cloud"]}}
    result = ProductRequirementsAnalysisAgent._evaluate_sop_conditionals(
        q_def, {"P1.deploy.a": "On-prem"}
    )
    assert result is False


def test_extract_sop_decisions_from_spec_empty_spec() -> None:
    """Empty spec should return empty decisions list."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._extract_sop_decisions_from_spec("")
    assert result == []
    llm.complete_json.assert_not_called()


def test_extract_sop_decisions_from_spec_success() -> None:
    """LLM returns valid decisions; verify SOPDecision parsing."""
    llm = _StubClient(
        {
            "extracted_decisions": [
                {
                    "sop_id": "P1.deploy.a",
                    "decision": "Cloud",
                    "confidence": 0.95,
                    "spec_excerpt": "Deploy on AWS",
                },
                {
                    "sop_id": "P1.coding.b",
                    "decision": "Python",
                    "confidence": 0.9,
                    "spec_excerpt": "Built with Python",
                },
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    decisions = agent._extract_sop_decisions_from_spec("Deploy on AWS. Built with Python.")
    assert len(decisions) == 2
    assert decisions[0].sop_id == "P1.deploy.a"
    assert decisions[0].decision == "Cloud"
    assert decisions[0].source == "spec"
    assert decisions[1].sop_id == "P1.coding.b"


def test_extract_sop_decisions_from_spec_low_confidence_filtered() -> None:
    """Low-confidence extractions should be filtered out."""
    llm = _StubClient(
        {
            "extracted_decisions": [
                {"sop_id": "P1.deploy.a", "decision": "Cloud", "confidence": 0.95},
                {"sop_id": "P1.data.b", "decision": "Maybe", "confidence": 0.3},
            ]
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    decisions = agent._extract_sop_decisions_from_spec("Some spec content")
    assert len(decisions) == 1
    assert decisions[0].sop_id == "P1.deploy.a"


def test_extract_sop_decisions_from_spec_llm_failure() -> None:
    """LLM failure should return empty list, not raise."""

    class _FailingClient(DummyLLMClient):
        def complete_json(self, prompt, **kwargs):
            raise RuntimeError("LLM unavailable")

    llm = _FailingClient()
    agent = ProductRequirementsAnalysisAgent(llm)
    decisions = agent._extract_sop_decisions_from_spec("Some spec content")
    assert decisions == []


def test_build_architecture_approval_questions() -> None:
    """Architecture approval builds questions for type + gaps."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    arch_result = ArchitectureAnalysisResult(
        architecture_type="3-tier",
        architecture_rationale="Good separation of concerns",
        tool_gaps=[
            ToolGapAnalysis(
                gap_description="No monitoring",
                recommendations=[
                    ToolRecommendation(name="Datadog", description="Full stack monitoring"),
                    ToolRecommendation(name="Prometheus", description="Open source metrics"),
                ],
            ),
        ],
    )
    questions = agent._build_architecture_approval_questions(arch_result)
    assert len(questions) == 2  # 1 architecture type + 1 gap
    assert questions[0].id == "arch_type_approval"
    assert "3-tier" in questions[0].question_text
    assert questions[1].id == "gap_0_selection"


def test_apply_architecture_approval_approve() -> None:
    """Approving architecture should keep original type."""
    arch_result = ArchitectureAnalysisResult(architecture_type="3-tier")
    answered = [
        AnsweredQuestion(
            question_id="arch_type_approval",
            question_text="Approve?",
            selected_answer="Approve 3-tier architecture",
        )
    ]
    ProductRequirementsAnalysisAgent._apply_architecture_approval(arch_result, answered)
    assert arch_result.architecture_type == "3-tier"


def test_apply_architecture_approval_modify() -> None:
    """Selecting 'different' with other_text should update type."""
    arch_result = ArchitectureAnalysisResult(architecture_type="3-tier")
    answered = [
        AnsweredQuestion(
            question_id="arch_type_approval",
            question_text="Approve?",
            selected_answer="Suggest a different architecture",
            other_text="microservices",
        )
    ]
    ProductRequirementsAnalysisAgent._apply_architecture_approval(arch_result, answered)
    assert arch_result.architecture_type == "microservices"


def test_apply_architecture_approval_gap_selection() -> None:
    """Gap selection should be recorded."""
    arch_result = ArchitectureAnalysisResult(
        tool_gaps=[
            ToolGapAnalysis(
                gap_description="No monitoring",
                recommendations=[
                    ToolRecommendation(name="Datadog"),
                    ToolRecommendation(name="Prometheus"),
                ],
            ),
        ],
    )
    answered = [
        AnsweredQuestion(
            question_id="gap_0_selection",
            question_text="Which monitoring?",
            selected_answer="Prometheus",
        )
    ]
    ProductRequirementsAnalysisAgent._apply_architecture_approval(arch_result, answered)
    assert arch_result.tool_gaps[0].selected_recommendation == "Prometheus"


def test_format_architecture_document() -> None:
    """Architecture document should contain key sections."""
    arch_result = ArchitectureAnalysisResult(
        architecture_type="3-tier",
        architecture_rationale="Good for this project",
        data_types_and_storage=[
            {
                "data_type": "User profiles",
                "recommended_store": "PostgreSQL",
                "rationale": "Relational",
            }
        ],
        task_types=[
            {"task": "API handling", "classification": "IO-bound", "compute_needs": "standard"}
        ],
        tool_gaps=[
            ToolGapAnalysis(
                gap_description="No CI/CD",
                recommendations=[
                    ToolRecommendation(name="GitHub Actions", description="Built-in CI")
                ],
                selected_recommendation="GitHub Actions",
            ),
        ],
        diagrams={"overview": "```mermaid\ngraph TD\n  A-->B\n```\n\nSystem overview."},
        summary="A 3-tier architecture is recommended.",
    )
    doc = ProductRequirementsAnalysisAgent._format_architecture_document(arch_result)
    assert "# Architecture Analysis" in doc
    assert "3-tier" in doc
    assert "PostgreSQL" in doc
    assert "IO-bound" in doc
    assert "GitHub Actions" in doc
    assert "mermaid" in doc
    assert "3-tier architecture is recommended" in doc


def test_sop_models_basic() -> None:
    """Basic SOPDecision, ToolRecommendation, ToolGapAnalysis, ArchitectureAnalysisResult instantiation."""
    decision = SOPDecision(
        sop_id="P1.deploy.a",
        sub_phase=SOPSubPhase.DEPLOYMENT,
        question_text="Where deployed?",
        decision="Cloud",
        source="spec",
    )
    assert decision.confidence == 1.0

    rec = ToolRecommendation(name="Datadog", description="Monitoring")
    assert rec.why_recommended == ""

    gap = ToolGapAnalysis(gap_description="No monitoring", recommendations=[rec])
    assert gap.selected_recommendation is None

    arch = ArchitectureAnalysisResult()
    assert arch.architecture_type == ""
    assert arch.diagrams == {}


# ---------------------------------------------------------------------------
# _assess_sub_phase_gaps tests
# ---------------------------------------------------------------------------


def test_assess_sub_phase_gaps_complete() -> None:
    """When LLM reports sub-phase as complete, returns (True, [])."""
    llm = _StubClient(
        {
            "is_complete": True,
            "completeness_rationale": "All deployment aspects covered.",
            "follow_up_questions": [],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Deploy on AWS with ECS containers.",
        [
            SOPDecision(
                sop_id="P1.deploy.a",
                sub_phase=SOPSubPhase.DEPLOYMENT,
                question_text="Where?",
                decision="AWS",
                source="spec",
            )
        ],
        {"P1.deploy.a": "AWS"},
    )
    assert is_complete is True
    assert follow_ups == []


def test_assess_sub_phase_gaps_incomplete_with_follow_ups() -> None:
    """When LLM reports gaps, returns (False, [OpenQuestion, ...])."""
    llm = _StubClient(
        {
            "is_complete": False,
            "completeness_rationale": "Missing region info.",
            "follow_up_questions": [
                {
                    "id": "P1.deploy.gen_1",
                    "question_text": "Which AWS region?",
                    "context": "Region affects latency.",
                    "category": "infrastructure",
                    "priority": "high",
                    "allow_multiple": False,
                    "sop_sub_phase": "deployment",
                    "options": [
                        {
                            "id": "opt_1",
                            "label": "us-east-1",
                            "is_default": True,
                            "rationale": "Common.",
                            "confidence": 0.8,
                        },
                        {
                            "id": "opt_2",
                            "label": "eu-west-1",
                            "is_default": False,
                            "rationale": "EU.",
                            "confidence": 0.5,
                        },
                        {
                            "id": "opt_3",
                            "label": "ap-southeast-1",
                            "is_default": False,
                            "rationale": "APAC.",
                            "confidence": 0.4,
                        },
                        {
                            "id": "opt_other",
                            "label": "Other",
                            "is_default": False,
                            "rationale": "Specify.",
                            "confidence": 0.3,
                        },
                    ],
                }
            ],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Deploy on AWS.",
        [],
        {},
    )
    assert is_complete is False
    assert len(follow_ups) == 1
    assert follow_ups[0].id == "P1.deploy.gen_1"
    assert follow_ups[0].question_text == "Which AWS region?"
    assert len(follow_ups[0].options) == 4


def test_assess_sub_phase_gaps_malformed_json() -> None:
    """Malformed LLM JSON should degrade gracefully to (True, [])."""
    llm = _StubClient("This is not valid JSON at all")
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Some spec.",
        [],
        {},
    )
    assert is_complete is True
    assert follow_ups == []


def test_assess_sub_phase_gaps_llm_exception() -> None:
    """LLM exception should degrade gracefully to (True, [])."""

    class _FailingClient(DummyLLMClient):
        def complete_json(self, prompt, **kwargs):
            raise RuntimeError("LLM unavailable")

    llm = _FailingClient()
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.SECURITY,
        "Some spec.",
        [],
        {},
    )
    assert is_complete is True
    assert follow_ups == []


def test_assess_sub_phase_gaps_duplicate_ids_skipped() -> None:
    """Follow-up questions with IDs already in decisions_map are skipped with a warning."""
    llm = _StubClient(
        {
            "is_complete": False,
            "completeness_rationale": "Gaps remain.",
            "follow_up_questions": [
                {
                    "id": "P1.deploy.a",
                    "question_text": "Duplicate question?",
                    "options": [
                        {
                            "id": "opt_1",
                            "label": "A",
                            "is_default": True,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                        {
                            "id": "opt_2",
                            "label": "B",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                        {
                            "id": "opt_3",
                            "label": "C",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                    ],
                },
                {
                    "id": "P1.deploy.gen_1",
                    "question_text": "New question?",
                    "options": [
                        {
                            "id": "opt_1",
                            "label": "X",
                            "is_default": True,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                        {
                            "id": "opt_2",
                            "label": "Y",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                        {
                            "id": "opt_3",
                            "label": "Z",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                    ],
                },
            ],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Some spec.",
        [
            SOPDecision(
                sop_id="P1.deploy.a",
                sub_phase=SOPSubPhase.DEPLOYMENT,
                question_text="Where?",
                decision="AWS",
                source="spec",
            )
        ],
        {"P1.deploy.a": "AWS"},
    )
    assert is_complete is False
    # Only the non-duplicate question should be returned
    assert len(follow_ups) == 1
    assert follow_ups[0].id == "P1.deploy.gen_1"


def test_assess_sub_phase_gaps_all_dupes_returns_empty_follow_ups() -> None:
    """When all LLM questions are duplicates, follow_ups is empty (loop will exit)."""
    llm = _StubClient(
        {
            "is_complete": False,
            "completeness_rationale": "Gaps remain.",
            "follow_up_questions": [
                {
                    "id": "P1.deploy.a",
                    "question_text": "Dupe 1?",
                    "options": [
                        {
                            "id": "o1",
                            "label": "A",
                            "is_default": True,
                            "rationale": ".",
                            "confidence": 0.5,
                        }
                    ],
                },
            ],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Spec.",
        [],
        {"P1.deploy.a": "AWS"},
    )
    assert is_complete is False
    assert follow_ups == []


def test_assess_sub_phase_gaps_options_padded_to_min_3() -> None:
    """When LLM returns < 3 options, they are padded to at least 3."""
    llm = _StubClient(
        {
            "is_complete": False,
            "completeness_rationale": "Gaps.",
            "follow_up_questions": [
                {
                    "id": "P1.deploy.gen_1",
                    "question_text": "Which compute model?",
                    "options": [
                        {
                            "id": "opt_1",
                            "label": "Serverless",
                            "is_default": True,
                            "rationale": ".",
                            "confidence": 0.8,
                        },
                    ],
                },
            ],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Spec.",
        [],
        {},
    )
    assert is_complete is False
    assert len(follow_ups) == 1
    opts = follow_ups[0].options
    assert len(opts) >= 3
    # Should have "Other" added
    assert any(o.label == "Other" for o in opts)


def test_assess_sub_phase_gaps_exactly_one_default() -> None:
    """After option padding/parsing, exactly one option has is_default=True."""
    llm = _StubClient(
        {
            "is_complete": False,
            "completeness_rationale": "Gaps.",
            "follow_up_questions": [
                {
                    "id": "P1.data.gen_1",
                    "question_text": "Which database?",
                    "options": [
                        {
                            "id": "opt_1",
                            "label": "PostgreSQL",
                            "is_default": True,
                            "rationale": ".",
                            "confidence": 0.8,
                        },
                        {
                            "id": "opt_2",
                            "label": "MySQL",
                            "is_default": True,
                            "rationale": ".",
                            "confidence": 0.6,
                        },
                        {
                            "id": "opt_3",
                            "label": "MongoDB",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.4,
                        },
                    ],
                },
            ],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    _, follow_ups = agent._assess_sub_phase_gaps(SOPSubPhase.DATA, "Spec.", [], {})
    assert len(follow_ups) == 1
    defaults = [o for o in follow_ups[0].options if o.is_default]
    assert len(defaults) == 1, f"Expected 1 default, got {len(defaults)}"


def test_assess_sub_phase_gaps_no_defaults_sets_first() -> None:
    """When LLM returns no default option, the first option becomes default."""
    llm = _StubClient(
        {
            "is_complete": False,
            "completeness_rationale": "Gaps.",
            "follow_up_questions": [
                {
                    "id": "P1.sec.gen_1",
                    "question_text": "Auth method?",
                    "options": [
                        {
                            "id": "opt_1",
                            "label": "OAuth2",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.7,
                        },
                        {
                            "id": "opt_2",
                            "label": "SAML",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.5,
                        },
                        {
                            "id": "opt_3",
                            "label": "API Keys",
                            "is_default": False,
                            "rationale": ".",
                            "confidence": 0.3,
                        },
                    ],
                },
            ],
        }
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    _, follow_ups = agent._assess_sub_phase_gaps(SOPSubPhase.SECURITY, "Spec.", [], {})
    assert len(follow_ups) == 1
    assert follow_ups[0].options[0].is_default is True
    # Only the first should be default
    defaults = [o for o in follow_ups[0].options if o.is_default]
    assert len(defaults) == 1


def test_assess_sub_phase_gaps_empty_llm_response() -> None:
    """Empty LLM response should degrade gracefully to (True, [])."""
    llm = _StubClient("")
    agent = ProductRequirementsAnalysisAgent(llm)
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.BUDGET,
        "Spec.",
        [],
        {},
    )
    assert is_complete is True
    assert follow_ups == []


def test_assess_sub_phase_gaps_passes_existing_ids_to_prompt() -> None:
    """Verify that existing question IDs are passed to the LLM prompt."""
    llm = _TrackingStubClient(
        {"is_complete": True, "completeness_rationale": "Done.", "follow_up_questions": []}
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        "Spec.",
        [
            SOPDecision(
                sop_id="P1.deploy.a",
                sub_phase=SOPSubPhase.DEPLOYMENT,
                question_text="Q",
                decision="A",
                source="user",
            )
        ],
        {"P1.deploy.a": "AWS", "P1.deploy.b": "ECS"},
    )
    # The prompt should contain the existing question IDs
    prompt = llm.last_prompt
    assert "P1.deploy.a" in prompt
    assert "P1.deploy.b" in prompt


def test_assess_sub_phase_gaps_survives_brace_bearing_spec() -> None:
    """Gap analysis must not raise when the spec contains curly braces.

    Preconditions: spec_content includes brace tokens that would break str.format,
        including a literal later template slot name such as ``{all_decisions}``.
    Postconditions: assess returns; the substituted prompt includes those tokens
        literally in the spec excerpt (one-pass substitution, no rescanning).
    """
    llm = _TrackingStubClient(
        {"is_complete": True, "completeness_rationale": "Done.", "follow_up_questions": []}
    )
    agent = ProductRequirementsAnalysisAgent(llm)
    brace_spec = (
        "Use template {curly} and also }unbalanced{ braces; document {all_decisions}."
    )
    is_complete, follow_ups = agent._assess_sub_phase_gaps(
        SOPSubPhase.DEPLOYMENT,
        brace_spec,
        [],
        {},
    )
    assert is_complete is True
    assert follow_ups == []
    assert llm.last_prompt is not None
    assert "{curly}" in llm.last_prompt
    assert "}unbalanced{" in llm.last_prompt
    # Spec excerpt must keep the literal token; it must not be overwritten by the
    # later decisions payload.
    assert "document {all_decisions}." in llm.last_prompt


def test_gap_analysis_and_context_prompt_invariants() -> None:
    """Prompt catalog invariants for the consolidated prompts cleanup.

    Preconditions: prompts module is importable.
    Postconditions: gap-analysis schema uses concrete booleans, a generic follow-up
        example (not AWS regions), and context-discovery schema includes source;
        unused architecture-approval prompt is absent.
    """
    from product_requirements_analysis_agent import prompts as pra_prompts

    gap = pra_prompts.SOP_SUB_PHASE_GAP_ANALYSIS_PROMPT
    assert '"is_complete": true/false' not in gap
    assert '"is_complete": false' in gap
    assert "Which AWS regions should the application be deployed in?" not in gap
    assert "us-east-1 (Virginia)" not in gap
    assert "What remaining detail is needed to close this gap for the sub-phase?" in gap

    ctx = pra_prompts.CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT
    assert '"source": "context_discovery"' in ctx

    consolidate = pra_prompts.CONSOLIDATE_QUESTIONS_PROMPT
    assert "source," in consolidate or "source, constraint_domain" in consolidate

    assert not hasattr(pra_prompts, "SOP_ARCHITECTURE_APPROVAL_PROMPT")
    arch = pra_prompts.SOP_ARCHITECTURE_ANALYSIS_PROMPT
    assert "Mermaid is allowed only inside diagrams" in arch


def test_max_gap_rounds_constant() -> None:
    """MAX_GAP_ROUNDS should be a reasonable limit smaller than MAX_SOP_ROUNDS."""
    assert MAX_GAP_ROUNDS == 3
    from product_requirements_analysis_agent.agent import MAX_SOP_ROUNDS

    assert MAX_GAP_ROUNDS < MAX_SOP_ROUNDS


# ---------------------------------------------------------------------------
# Dedicated unit tests for the helpers extracted during the run_workflow
# decomposition: _call_llm_text, _call_llm_json, _run_phase,
# _run_consistency_loops.
# ---------------------------------------------------------------------------


def test_call_llm_text_returns_stripped_model_text() -> None:
    """_call_llm_text returns the model's response coerced to str and stripped."""
    agent = ProductRequirementsAnalysisAgent(_StubClient("  # Spec body  "))
    assert agent._call_llm_text("prompt") == "# Spec body"


def test_call_llm_text_rejects_empty_or_non_string_prompt() -> None:
    """_call_llm_text raises ValueError (not assert) on an invalid prompt."""
    agent = ProductRequirementsAnalysisAgent(_StubClient("ok"))
    with pytest.raises(ValueError):
        agent._call_llm_text("")
    with pytest.raises(ValueError):
        agent._call_llm_text(None)  # type: ignore[arg-type]


def test_call_llm_json_parses_object() -> None:
    """_call_llm_json returns the parsed dict when the model emits a JSON object."""
    # Pass the model output as an explicit JSON *string* (what a real model
    # emits on the wire) rather than relying on the stub serializing a dict.
    agent = ProductRequirementsAnalysisAgent(_StubClient('{"consolidated_questions": []}'))
    assert agent._call_llm_json("prompt") == {"consolidated_questions": []}


def test_call_llm_json_returns_none_on_unparseable_output() -> None:
    """_call_llm_json returns None (never raises) when the response is not JSON."""
    agent = ProductRequirementsAnalysisAgent(_StubClient("not valid json {"))
    assert agent._call_llm_json("prompt") is None


def test_call_llm_json_parses_markdown_fenced_object() -> None:
    """_call_llm_json recovers a JSON object wrapped in a ```json fence."""
    fenced = "```json\n" + json.dumps({"key": "value"}) + "\n```"
    agent = ProductRequirementsAnalysisAgent(_StubClient(fenced))
    assert agent._call_llm_json("prompt") == {"key": "value"}


def test_run_phase_returns_value_on_success() -> None:
    """_run_phase returns (True, fn()) and leaves failure_reason unset on success."""
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    ok, value = agent._run_phase(result, "Spec review", lambda: 42)
    assert ok is True
    assert value == 42
    assert not result.failure_reason


def test_run_phase_captures_failure_with_name_prefix() -> None:
    """_run_phase swallows the exception, sets a name-prefixed failure_reason, returns (False, None)."""
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()

    def _boom() -> None:
        raise ValueError("nope")

    ok, value = agent._run_phase(result, "Spec update", _boom)
    assert ok is False
    assert value is None
    assert result.failure_reason == "Spec update failed: nope"


def _single_open_question() -> OpenQuestion:
    return OpenQuestion(
        id="q1",
        question_text="Which auth?",
        options=[
            QuestionOption(id="o1", label="OAuth", is_default=True, rationale="", confidence=0.5)
        ],
    )


def test_run_consistency_loops_noop_below_threshold(tmp_path: Path) -> None:
    """When reduction_ratio is below the threshold, no pass runs and inputs are returned unchanged."""
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    sr = SpecReviewResult(issues=[], gaps=[], open_questions=[_single_open_question()], summary="")
    with patch.object(agent, "_update_spec_for_consistency_and_clarity") as upd:
        out_sr, out_spec, out_count = agent._run_consistency_loops(
            result=result,
            current_spec="# Spec",
            spec_review_result=sr,
            open_count=1,
            reduction_ratio=0.0,  # below DEDUP_REDUCTION_THRESHOLD → loop body never runs
            count_before_dedup=1,
            deduped_questions=[_single_open_question()],
            repo_path=tmp_path,
            iteration=1,
            base_version=1,
            all_answered_questions=[],
            update_job=lambda **_k: None,
            on_chunk_progress=lambda _a, _b: None,
        )
    upd.assert_not_called()
    assert out_sr is sr
    assert out_spec == "# Spec"
    assert out_count == 1


def test_run_consistency_loops_runs_one_pass_then_exits(tmp_path: Path) -> None:
    """A single consistency pass re-reviews, re-dedupes to empty, and exits with open_count 0."""
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    sr_initial = SpecReviewResult(
        issues=[], gaps=[], open_questions=[_single_open_question()], summary=""
    )
    sr_after = SpecReviewResult(
        issues=[], gaps=[], open_questions=[_single_open_question()], summary=""
    )
    with (
        patch.object(agent, "_read_qa_history", return_value="qa"),
        patch.object(
            agent, "_update_spec_for_consistency_and_clarity", return_value="# Spec v2"
        ) as upd,
        patch.object(agent, "_run_spec_review", return_value=(sr_after, "# Spec v2")),
        patch.object(agent, "_consolidate_open_questions", side_effect=lambda qs: list(qs)),
        patch.object(agent, "_dedupe_questions_by_answer_similarity", return_value=[]),
    ):
        out_sr, out_spec, out_count = agent._run_consistency_loops(
            result=result,
            current_spec="# Spec",
            spec_review_result=sr_initial,
            open_count=1,
            reduction_ratio=1.0,  # at/above threshold → enter the loop
            count_before_dedup=1,
            deduped_questions=[],
            repo_path=tmp_path,
            iteration=1,
            base_version=1,
            all_answered_questions=[],
            update_job=lambda **_k: None,
            on_chunk_progress=lambda _a, _b: None,
        )
    upd.assert_called_once()
    assert out_spec == "# Spec v2"
    assert out_count == 0
    assert out_sr.open_questions == []
    assert result.spec_review_result is not None


def test_run_consistency_loops_runs_multiple_passes(tmp_path: Path) -> None:
    """When each pass keeps reducing questions above the threshold, the loop runs
    again; it terminates once dedup empties the question set."""
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    sr_pass1 = SpecReviewResult(
        issues=[],
        gaps=[],
        open_questions=[_single_open_question(), _single_open_question()],
        summary="",
    )
    sr_pass2 = SpecReviewResult(
        issues=[], gaps=[], open_questions=[_single_open_question()], summary=""
    )
    with (
        patch.object(agent, "_read_qa_history", return_value="qa"),
        patch.object(
            agent, "_update_spec_for_consistency_and_clarity", side_effect=["# v2", "# v3"]
        ) as upd,
        patch.object(
            agent, "_run_spec_review", side_effect=[(sr_pass1, "# v2"), (sr_pass2, "# v3")]
        ),
        patch.object(agent, "_consolidate_open_questions", side_effect=lambda qs: list(qs)),
        # Pass 1: 2 → 1 question (ratio 0.5 ≥ threshold → loop again).
        # Pass 2: 1 → 0 questions (loop breaks on empty open_questions).
        patch.object(
            agent,
            "_dedupe_questions_by_answer_similarity",
            side_effect=[[_single_open_question()], []],
        ),
    ):
        out_sr, out_spec, out_count = agent._run_consistency_loops(
            result=result,
            current_spec="# v1",
            spec_review_result=SpecReviewResult(
                issues=[],
                gaps=[],
                open_questions=[_single_open_question(), _single_open_question()],
                summary="",
            ),
            open_count=2,
            reduction_ratio=1.0,
            count_before_dedup=2,
            deduped_questions=[_single_open_question()],
            repo_path=tmp_path,
            iteration=1,
            base_version=1,
            all_answered_questions=[],
            update_job=lambda **_k: None,
            on_chunk_progress=lambda _a, _b: None,
        )
    assert upd.call_count == 2  # two consistency passes ran
    assert out_count == 0
    assert out_spec == "# v3"
    assert out_sr.open_questions == []


def test_run_context_discovery_noop_when_job_id_none(tmp_path: Path) -> None:
    """With job_id None, context discovery is a no-op that returns the spec unchanged."""
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    ok, spec = agent._run_context_discovery(
        current_spec="# Original",
        repo_path=tmp_path,
        job_id=None,
        product_analysis_dir=tmp_path,
        all_answered_questions=[],
        result=result,
        update_job=lambda **_k: None,
    )
    assert ok is True
    assert spec == "# Original"


def test_run_context_discovery_resumes_from_validated_spec(tmp_path: Path) -> None:
    """When prior PRA artifacts exist, discovery is skipped and current_spec is loaded
    from validated_spec.md, with the phase set to SPEC_REVIEW."""
    pa_dir = tmp_path / "plan" / "product_analysis"
    pa_dir.mkdir(parents=True)
    (pa_dir / "validated_spec.md").write_text("# Resumed spec", encoding="utf-8")
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    ok, spec = agent._run_context_discovery(
        current_spec="# Original",
        repo_path=tmp_path,
        job_id="job-1",
        product_analysis_dir=pa_dir,
        all_answered_questions=[],
        result=result,
        update_job=lambda **_k: None,
    )
    assert ok is True
    assert spec == "# Resumed spec"
    assert result.current_phase == AnalysisPhase.SPEC_REVIEW


def test_run_context_discovery_resume_falls_back_to_latest_updated_spec(tmp_path: Path) -> None:
    """Without validated_spec.md, resume loads the highest-versioned updated_spec_v*.md."""
    pa_dir = tmp_path / "plan" / "product_analysis"
    pa_dir.mkdir(parents=True)
    (pa_dir / "updated_spec_v3.md").write_text("# v3", encoding="utf-8")
    (pa_dir / "updated_spec_v11.md").write_text("# v11", encoding="utf-8")
    agent = ProductRequirementsAnalysisAgent(_StubClient({}))
    result = AnalysisWorkflowResult()
    ok, spec = agent._run_context_discovery(
        current_spec="# Original",
        repo_path=tmp_path,
        job_id="job-1",
        product_analysis_dir=pa_dir,
        all_answered_questions=[],
        result=result,
        update_job=lambda **_k: None,
    )
    assert ok is True
    assert spec == "# v11"  # version 11 sorts above version 3


def test_filter_duplicate_questions_strips_punctuation_before_stemming() -> None:
    """A question token with attached punctuation (e.g. 'store?') should still match
    the unpunctuated form of the same word in qa_history, so the question is filtered
    as a duplicate instead of being re-asked."""
    questions = [OpenQuestion(id="q1", question_text="Where do we store? the data")]
    qa_history = "Q: Where should data be stored?\nA: We store data in Postgres."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_keeps_non_duplicate() -> None:
    """A question with no overlapping stems in qa_history is not filtered out."""
    questions = [OpenQuestion(id="q1", question_text="Which cloud provider should we use?")]
    qa_history = "Q: What is the deployment target?\nA: On-premise servers."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == questions
    assert duplicates == []


def test_filter_duplicate_questions_stopword_only_question_is_kept() -> None:
    """A question with no content-bearing word (only stopwords) is kept as
    filtered rather than treated as a duplicate, since there's no keyword
    evidence either way."""
    questions = [OpenQuestion(id="q1", question_text="Should we?")]
    qa_history = "Q: Should we use OAuth2?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == questions
    assert duplicates == []


def test_filter_duplicate_questions_stems_plural_and_past_tense() -> None:
    """Plural ('options'->'option') and past-tense ('documented'->'document')
    stemming let a duplicate match against the base word form recorded in
    qa_history."""
    questions = [
        OpenQuestion(id="q1", question_text="Where are the config options documented for the service?")
    ]
    qa_history = "Q: Should we document the config option for the service?\nA: Yes, use Confluence."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_silent_e_past_tense() -> None:
    """A past-tense word formed from a silent-e root (e.g. 'based' from
    'base') must match the root word in qa_history.

    The question/history pair share no other content word, so this can only
    pass if the stemmer actually derives 'base' from 'based' (blindly
    stripping the 'ed' suffix, based -> 'bas', would never match 'base').
    """
    questions = [OpenQuestion(id="q1", question_text="Is it based?")]
    qa_history = "Q: Is it base?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_silent_e_past_tense_reverse_direction() -> None:
    """The same silent-e match must also work in reverse: a root-form
    keyword ('base') matching an inflected history word ('based').

    This is the direction a single optional 's'/'ed' history-side suffix
    cannot cover (stem "base" + literal "ed" = "baseed", not "based"), so it
    requires stemming the history word itself, not just the keyword.
    """
    questions = [OpenQuestion(id="q1", question_text="Is it base?")]
    qa_history = "Q: Is it based?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_es_plural_dropping_e() -> None:
    """A plural formed by adding '-es' to a root with no silent 'e' (e.g.
    'classes' from 'class') must match the root word in qa_history.

    The question/history pair share no other content word, so this can only
    pass if the stemmer actually derives 'class' from 'classes' (stripping
    only the trailing 's', classes -> 'classe', would never match 'class').
    """
    questions = [OpenQuestion(id="q1", question_text="Are there classes?")]
    qa_history = "Q: Are there class?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_es_plural_reverse_direction() -> None:
    """The same '-es' plural match must also work in reverse: a root-form
    keyword ('class') matching an inflected history word ('classes')."""
    questions = [OpenQuestion(id="q1", question_text="Is it a class?")]
    qa_history = "Q: Are these classes?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_ies_plural() -> None:
    """A plural formed by '-ies' (e.g. 'policies' from 'policy') must match
    the root word in qa_history, with no other shared content word."""
    questions = [OpenQuestion(id="q1", question_text="What policies are there?")]
    qa_history = "Q: What policy is there?\nA: One."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_ies_plural_reverse_direction() -> None:
    """The same '-ies' plural match must also work in reverse: a root-form
    keyword ('policy') matching an inflected history word ('policies')."""
    questions = [OpenQuestion(id="q1", question_text="What is the policy?")]
    qa_history = "Q: What are the policies?\nA: Retry logic."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_ied_verb() -> None:
    """A '-ied' past tense verb (e.g. 'studied' from 'study') must match the
    root word in qa_history, in both directions."""
    questions = [OpenQuestion(id="q1", question_text="Was this studied?")]
    qa_history = "Q: Did we study this?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_stems_ied_verb_reverse_direction() -> None:
    questions = [OpenQuestion(id="q1", question_text="Did they study this?")]
    qa_history = "Q: Was this studied?\nA: Yes."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_stem_candidates_double_s_word_returns_only_itself() -> None:
    """A word ending in a doubled 's' (e.g. 'address', 'process') is never
    treated as a plural/past-tense form: no shortened candidate (e.g.
    'addres') is ever generated that could accidentally match an unrelated
    word in qa_history."""
    assert _stem_candidates("address") == {"address"}
    assert _stem_candidates("process") == {"process"}
    assert _stem_candidates("success") == {"success"}


def test_filter_duplicate_questions_double_s_word_is_not_stemmed() -> None:
    """A word ending in a doubled 's' (e.g. 'address') is still matched
    against its literal and regularly-suffixed forms in qa_history."""
    questions = [OpenQuestion(id="q1", question_text="What should the email address format be?")]
    qa_history = "Q: What email format and address rules apply?\nA: Follow RFC 5322 for addresses."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_matches_short_content_word_keywords() -> None:
    """A question made up entirely of short (<=3 char) content words is still
    recognized as a duplicate.

    Previously key_stems filtered to len(_clean_token(w)) > 3, so a question
    with no word over 3 characters (e.g. an all-short-acronym question) never
    reached the extract_answer_from_qa_history extractor as a duplicate
    candidate at all, regardless of how permissive the extractor itself is.
    """
    questions = [OpenQuestion(id="q1", question_text="Do we use IAM or ACL on S3?")]
    qa_history = "Q: Should we use IAM or ACL policies on S3 buckets?\nA: Use IAM policies exclusively."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == []
    assert duplicates == questions


def test_filter_duplicate_questions_short_keyword_does_not_match_as_a_substring() -> None:
    """A short key stem must match a whole word in qa_history, not merely
    appear as a substring inside an unrelated longer word.

    Once short (<=3 char) words became eligible key stems, comparing them via
    raw substring containment risked a false match: the stem "api" is a
    substring of "capitalizing", which would incorrectly mark an unrelated
    question as an already-answered duplicate and drop it from the questions
    asked of the user.
    """
    questions = [OpenQuestion(id="q1", question_text="Do we use an API?")]
    qa_history = "Q: Are we capitalizing gains this quarter?\nA: Unrelated topic."

    filtered, duplicates = filter_duplicate_questions(questions, qa_history)

    assert filtered == questions
    assert duplicates == []


def test_filter_duplicate_questions_and_extractor_agree_on_short_keyword_duplicate() -> None:
    """filter_duplicate_questions and extract_answer_from_qa_history are consistent
    for a short-keyword question, matching the actual spec-review call chain
    (spec_review.filter_duplicate_questions -> spec_writing.update_spec_from_duplicates
    -> qa_history.extract_answer_from_qa_history).

    A short-keyword question that the extractor can answer must also be
    recognized as a duplicate candidate upstream, or it never reaches the
    extractor and is re-asked regardless of the extractor's own behavior.
    """
    qa_history = (
        "# Q&A History\n\n"
        "## Iteration 1\n\n"
        "### Should we use IAM or ACL policies on S3 buckets?\n"
        "**Answer:** Use IAM policies exclusively.\n\n"
    )
    question = OpenQuestion(id="q1", question_text="Do we use IAM or ACL on S3?")

    filtered, duplicates = filter_duplicate_questions([question], qa_history)
    assert filtered == []
    assert duplicates == [question]

    result = extract_answer_from_qa_history(question, qa_history)
    assert result is not None
    assert result.selected_answer == "Use IAM policies exclusively."
