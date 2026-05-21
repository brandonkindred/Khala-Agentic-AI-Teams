"""Test the simplest possible run_pipeline path with all heavy work mocked.

We replace the writer / copy-editor / planning / publication agents with
deterministic stubs so the orchestrator runs end-to-end in milliseconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _make_plan():
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        PlanningPhaseResult,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    return PlanningPhaseResult(
        content_plan=plan,
        planning_iterations_used=1,
        parse_retry_count=0,
        planning_wall_ms_total=10.0,
    )


def test_run_pipeline_no_gates_no_job(monkeypatch, tmp_path: Path) -> None:
    """Smallest possible orchestration: planning + draft, no gates, no job_id."""
    import agent_implementations.blog_writing_process_v2 as v2

    # Stub heavy steps:
    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: _make_plan())

    from blog_writer_agent.models import WriterOutput

    class _StubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return WriterOutput(draft="# Draft\n\nBody.")

        def revise(self, *a, **kw):
            return WriterOutput(draft="# Revised\n\nBody.")

        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft="# Revised\n\nBody.")

        def identify_uncertainty_questions(self, *a, **kw):
            return []

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return "stuck"

    from blog_copy_editor_agent.models import CopyEditorOutput

    class _StubEditor:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return CopyEditorOutput(approved=True, summary="ok", feedback_items=[])

    monkeypatch.setattr(v2, "BlogWriterAgent", _StubWriter)
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", _StubEditor)

    # Style and brand spec — non-empty strings so the missing-guideline check passes
    monkeypatch.setattr(v2, "load_style_file", lambda path, label="": "guidelines text")

    from blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="Topic about AI", audience="devs", max_results=10)
    work_dir = tmp_path / "wd"
    planning_phase, draft_result, status = v2.run_pipeline(
        brief,
        work_dir=work_dir,
        run_gates=False,
        draft_editor_iterations=1,
        llm_client=object(),  # dummy
    )
    assert status == "PASS"
    assert draft_result.draft.startswith("# Draft")
    assert planning_phase.content_plan.overarching_topic == "Topic"


def test_run_pipeline_no_gates_no_workdir(monkeypatch) -> None:
    """No work_dir — artifact writes are skipped."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: _make_plan())

    from blog_writer_agent.models import WriterOutput

    class _StubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return WriterOutput(draft="# Draft")

        def revise(self, *a, **kw):
            return WriterOutput(draft="# Draft")

        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft="# Draft")

        def identify_uncertainty_questions(self, *a, **kw):
            return []

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return ""

    from blog_copy_editor_agent.models import CopyEditorOutput

    class _StubEditor:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return CopyEditorOutput(approved=True, summary="ok", feedback_items=[])

    monkeypatch.setattr(v2, "BlogWriterAgent", _StubWriter)
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", _StubEditor)
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "ok")

    from blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="hi", max_results=5)
    _, draft, status = v2.run_pipeline(
        brief,
        work_dir=None,
        run_gates=False,
        draft_editor_iterations=1,
    )
    assert status == "PASS"


def test_run_pipeline_missing_guidelines_raises(monkeypatch, tmp_path: Path) -> None:
    """When style/brand files load as empty, DraftError is raised before any drafting."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: _make_plan())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "")

    from blog_research_agent.models import ResearchBriefInput
    from shared.errors import DraftError

    brief = ResearchBriefInput(brief="hi", max_results=5)
    with pytest.raises(DraftError):
        v2.run_pipeline(
            brief,
            work_dir=tmp_path / "wd",
            run_gates=False,
            draft_editor_iterations=1,
        )


def test_run_pipeline_copy_editor_stalls_then_accepts(monkeypatch, tmp_path: Path) -> None:
    """Copy editor never approves; eventually accept after iterations exhausted."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: _make_plan())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "ok")

    from blog_writer_agent.models import WriterOutput

    class _StubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return WriterOutput(draft="# Draft\n\nBody.")

        def revise(self, *a, **kw):
            return WriterOutput(draft="# Revised\n\nBody.")

        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft="# Revised\n\nBody.")

        def identify_uncertainty_questions(self, *a, **kw):
            return []

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return ""

    from blog_copy_editor_agent.models import CopyEditorOutput, FeedbackItem

    class _StubEditor:
        def __init__(self, *a, **kw):
            pass

        def run(self, *a, **kw):
            return CopyEditorOutput(
                approved=False,
                summary="needs work",
                feedback_items=[
                    FeedbackItem(
                        category="grammar",
                        severity="minor",
                        location="para 1",
                        issue="comma",
                    )
                ],
            )

    monkeypatch.setattr(v2, "BlogWriterAgent", _StubWriter)
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", _StubEditor)

    from blog_research_agent.models import ResearchBriefInput

    brief = ResearchBriefInput(brief="hi", max_results=5)
    # Use small draft_editor_iterations so the loop completes quickly
    _, draft, status = v2.run_pipeline(
        brief,
        work_dir=tmp_path / "wd",
        run_gates=False,
        draft_editor_iterations=3,
    )
    # Even with copy editor never approving, pipeline finishes (loop exhausts).
    assert status == "PASS"
