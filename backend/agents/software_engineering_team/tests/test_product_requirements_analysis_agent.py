"""Tests for the Product Requirements Analysis agent."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent
from product_requirements_analysis_agent.agent import _context_discovery_fallback_questions
from product_requirements_analysis_agent.models import (
    AnsweredQuestion,
    OpenQuestion,
    QuestionOption,
    SpecCleanupResult,
    SpecReviewResult,
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
    assert "Auto-answered" in out or "85%" in out
    assert "### Second question?" in out
    assert "**Answer:** No" in out
    assert "Default applied" in out
    assert "Custom text:" in out
    assert "Custom note" in out


def test_run_spec_review_invokes_llm_once(tmp_path: Path) -> None:
    """_run_spec_review performs a single LLM call (whole-spec review, no chunking)."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    llm = MagicMock()
    llm.complete_json_with_continuation.return_value = {
        "issues": [],
        "gaps": [],
        "open_questions": [],
        "summary": "Done",
    }
    agent = ProductRequirementsAnalysisAgent(llm)
    agent._context_files = {}
    result, updated_spec = agent._run_spec_review(
        spec_content="# My Spec\n\n## Section\nContent",
        repo_path=tmp_path,
        answered_questions=None,
    )
    assert llm.complete_json_with_continuation.call_count == 1
    assert result.summary == "Done"
    assert updated_spec == "# My Spec\n\n## Section\nContent"


def test_run_spec_review_includes_qa_in_prompt(tmp_path: Path) -> None:
    """When answered_questions is non-empty, the prompt passed to the LLM contains Q&A text."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    llm = MagicMock()
    llm.complete_json_with_continuation.return_value = {
        "issues": [],
        "gaps": [],
        "open_questions": [],
        "summary": "Done",
    }
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
    call_args = llm.complete_json_with_continuation.call_args
    prompt = call_args[0][0]
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
    llm = MagicMock()
    llm.complete_json_with_continuation.return_value = {
        "issues": [],
        "gaps": [],
        "open_questions": [],
        "summary": "Done",
    }
    agent = ProductRequirementsAnalysisAgent(llm)
    agent._context_files = {}
    agent._run_spec_review(
        spec_content="# Spec",
        repo_path=tmp_path,
        answered_questions=None,
    )
    call_args = llm.complete_json_with_continuation.call_args
    prompt = call_args[0][0]
    assert "OAuth provider?" in prompt
    assert "GitHub" in prompt


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


def test_run_workflow_re_runs_spec_review_after_clarification(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When _run_spec_review returns a different spec (clarification), re-run spec review on clarified spec and log it."""
    caplog.set_level(logging.INFO)
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)

    one_question = OpenQuestion(
        id="q1",
        question_text="Which OAuth provider?",
        options=[QuestionOption(id="opt1", label="GitHub", is_default=True, rationale="", confidence=0.9)],
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

    with patch.object(agent, "_run_context_constraints_discovery", return_value=[]):
        with patch.object(agent, "_run_spec_review", side_effect=run_spec_review):
            with patch.object(agent, "_communicate_with_user") as mock_comm:
                mock_comm.return_value = [
                    AnsweredQuestion(question_id="q1", question_text="Which OAuth provider?", selected_answer="GitHub")
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
    assert len(run_spec_review_calls) == 2, "Should call _run_spec_review twice (initial + re-run after clarification)"
    assert run_spec_review_calls[0] == "# Original spec"
    assert run_spec_review_calls[1] == "# Clarified spec"
    assert any(
        "Re-ran spec review on clarified spec" in rec.message for rec in caplog.records
    ), "Should log that spec review was re-run after clarification"


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

    with patch.object(agent, "_run_context_constraints_discovery", return_value=[]):
        with patch.object(agent, "_run_spec_review", return_value=(spec_review_no_questions, "# Spec")):
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
    assert validated_text == cleaned_spec_content, "validated_spec.md should contain the cleaned spec"
    assert prd_text == prd_content, "product_requirements_document.md should contain the PRD"
    assert validated_text != prd_text, "validated spec and PRD must differ"
    assert "Executive Summary" in prd_text, "PRD should contain PRD template sections"
    assert "Executive Summary" not in validated_text, "validated spec is cleaned spec, not the full PRD"


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
            options=[QuestionOption(id="opt_paas", label="PaaS", is_default=True, rationale="", confidence=0.7)],
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
            options=[QuestionOption(id="opt_oauth", label="OAuth", is_default=True, rationale="", confidence=0.8)],
        )
    ]
    pending = agent._convert_to_pending_questions(open_questions)
    assert "Recommendation:" in pending[0]["context"]
    assert "We recommend OAuth with a single provider" in pending[0]["context"]


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
    llm = MagicMock()
    llm.complete_json.return_value = {
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
                    {"id": "opt_oauth", "label": "OAuth (e.g. Google)", "is_default": True, "rationale": "Simple", "confidence": 0.8},
                    {"id": "opt_sso", "label": "Enterprise SSO", "is_default": False, "rationale": "Enterprise", "confidence": 0.5},
                ],
            }
        ]
    }
    agent = ProductRequirementsAnalysisAgent(llm)
    q1 = OpenQuestion(
        id="q1",
        question_text="Do you want Google only for OAuth?",
        options=[QuestionOption(id="o1", label="Yes", is_default=True, rationale="", confidence=0.5)],
    )
    q2 = OpenQuestion(
        id="q2",
        question_text="What is the right provider? OAuth or Enterprise?",
        options=[QuestionOption(id="o2", label="OAuth", is_default=True, rationale="", confidence=0.5)],
    )
    result = agent._consolidate_open_questions([q1, q2])
    assert len(result) == 1
    assert result[0].id == "auth_approach"
    assert "authentication approach" in result[0].question_text
    assert len(result[0].options) == 2
    assert result[0].options[0].id == "opt_oauth"
    assert result[0].options[1].id == "opt_sso"


def test_review_question_answer_alignment_parses_llm_output_and_preserves_ids() -> None:
    """_review_question_answer_alignment should return List[OpenQuestion] with same ids when LLM returns valid aligned_questions."""
    llm = MagicMock()
    llm.complete_json.return_value = {
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
                    {"id": "opt_paas", "label": "PaaS (Heroku, Render)", "is_default": True, "rationale": "", "confidence": 0.7},
                ],
            }
        ]
    }
    agent = ProductRequirementsAnalysisAgent(llm)
    q = OpenQuestion(
        id="infra_q",
        question_text="What platform category for deployment?",
        options=[QuestionOption(id="opt_paas", label="PaaS", is_default=True, rationale="", confidence=0.7)],
    )
    result = agent._review_question_answer_alignment([q])
    assert len(result) == 1
    assert result[0].id == "infra_q"
    assert result[0].question_text == "What platform category for deployment?"


def test_dedupe_questions_by_similarity_keeps_first_and_drops_near_duplicates() -> None:
    """_dedupe_questions_by_similarity drops questions similar to an earlier one, preserves order."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    opt = QuestionOption(id="o1", label="Yes", is_default=True, rationale="", confidence=0.9)
    q1 = OpenQuestion(id="a", question_text="Which OAuth provider?", options=[opt])
    q2 = OpenQuestion(id="b", question_text="Which oauth provider?", options=[opt])
    q3 = OpenQuestion(id="c", question_text="Where to deploy?", options=[opt])
    result = agent._dedupe_questions_by_similarity([q1, q2, q3])
    assert len(result) == 2
    assert result[0].id == "a"
    assert result[1].id == "c"


def test_filter_organizational_questions_removes_org_keeps_technical() -> None:
    """_filter_organizational_questions removes organizational/process questions and keeps technical ones."""
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    opt = QuestionOption(id="o1", label="Option", is_default=True, rationale="", confidence=0.8)
    q_org = OpenQuestion(
        id="org1",
        question_text="What is the process for making a decision?",
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


def test_run_context_constraints_discovery_returns_questions_when_llm_valid(
    tmp_path: Path,
) -> None:
    """_run_context_constraints_discovery returns non-empty List[OpenQuestion] when LLM returns valid JSON."""
    llm = MagicMock()
    llm.complete_text.return_value = '''{
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
    }'''
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._run_context_constraints_discovery("# Spec", tmp_path)
    assert len(result) >= 1
    assert result[0].id == "ctx_project_type"
    assert "organization" in result[0].question_text
    assert result[0].source == "context_discovery"
    assert len(result[0].options) == 2


def test_run_context_constraints_discovery_uses_fallback_on_llm_failure(
    tmp_path: Path,
) -> None:
    """_run_context_constraints_discovery uses fixed fallback when LLM raises or returns empty/invalid."""
    llm = MagicMock()
    llm.complete_text.side_effect = Exception("LLM unavailable")
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._run_context_constraints_discovery("# Spec", tmp_path)
    fallback = _context_discovery_fallback_questions()
    assert len(result) == len(fallback)
    assert all(q.source == "context_discovery" for q in result)
    ids = [q.id for q in result]
    assert "ctx_project_type" in ids
    assert "ctx_deployment" in ids
    assert "ctx_sla" in ids


def test_run_context_constraints_discovery_uses_fallback_on_empty_json(tmp_path: Path) -> None:
    """_run_context_constraints_discovery uses fallback when LLM returns empty open_questions."""
    llm = MagicMock()
    llm.complete_text.return_value = '{"open_questions": []}'
    agent = ProductRequirementsAnalysisAgent(llm)
    result = agent._run_context_constraints_discovery("# Spec", tmp_path)
    fallback = _context_discovery_fallback_questions()
    assert len(result) == len(fallback)


def test_inject_context_answers_into_spec_prepends_section(tmp_path: Path) -> None:
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
    result = agent._inject_context_answers_into_spec(current_spec, answered, tmp_path)
    assert result.startswith("## Project context and constraints")
    assert "What type of organization?" in result
    assert "Startup" in result
    assert "Where to deploy?" in result
    assert "Cloud" in result
    assert "# Original spec" in result
    assert "Some content." in result


def test_run_workflow_skips_context_discovery_when_no_job_id(tmp_path: Path) -> None:
    """run_workflow with job_id=None does not call _run_context_constraints_discovery; proceeds to spec review."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    spec_review_no_questions = SpecReviewResult(
        summary="Complete", issues=[], gaps=[], open_questions=[]
    )
    cleanup_result = SpecCleanupResult(
        is_valid=True, validation_issues=[], cleaned_spec="# Cleaned", summary="Done"
    )
    llm = MagicMock()
    agent = ProductRequirementsAnalysisAgent(llm)
    with patch.object(agent, "_run_context_constraints_discovery") as mock_context:
        with patch.object(agent, "_run_spec_review", return_value=(spec_review_no_questions, "# Spec")):
            with patch.object(agent, "_run_spec_cleanup", return_value=cleanup_result):
                with patch.object(agent, "_generate_prd_document", return_value="# PRD"):
                    agent.run_workflow(
                        spec_content="# Spec",
                        repo_path=tmp_path,
                        job_id=None,
                        job_updater=lambda **kw: None,
                    )
    mock_context.assert_not_called()


def test_run_workflow_with_context_discovery_injects_into_spec(tmp_path: Path) -> None:
    """With job_id set, context discovery runs; first spec review receives spec that includes injected context section."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    context_questions = [
        OpenQuestion(
            id="ctx_deploy",
            question_text="Where to deploy?",
            options=[QuestionOption(id="opt_cloud", label="Cloud", is_default=True, rationale="", confidence=0.8)],
        )
    ]
    context_answered = [
        AnsweredQuestion(
            question_id="ctx_deploy",
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

    with patch.object(agent, "_run_context_constraints_discovery", return_value=context_questions):
        with patch.object(agent, "_communicate_with_user", return_value=context_answered):
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
    assert first_spec.startswith("## Project context and constraints")
    assert "Where to deploy?" in first_spec
    assert "Cloud" in first_spec
    # qa_history should contain context Q&A (iteration 0)
    qa_file = tmp_path / "plan" / "product_analysis" / "qa_history.md"
    assert qa_file.exists()
    content = qa_file.read_text(encoding="utf-8")
    assert "Where to deploy?" in content
    assert "Cloud" in content
    assert "Iteration 0" in content
