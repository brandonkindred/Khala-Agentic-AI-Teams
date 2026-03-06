"""Tests for the Product Requirements Analysis agent."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from product_requirements_analysis_agent import ProductRequirementsAnalysisAgent
from product_requirements_analysis_agent.models import (
    AnsweredQuestion,
    OpenQuestion,
    QuestionOption,
    SpecCleanupResult,
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
