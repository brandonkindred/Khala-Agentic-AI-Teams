"""Tests for ``run_planning_stage``'s ``ctx.covered_sections`` accumulation.

Covers the two sources a plan section's narrative can come from -- a fresh
ghost-writer interview and a story-bank hit -- and pins that the resulting set
is derived from ``collected_story_pairs``/the bank results directly, never by
substring-matching ``elicited_stories_text``; that whitespace-only narratives
and both sources' failures are handled per the pre-existing best-effort
guards; and that an empty run reproduces today's behavior exactly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ._content_plan_test_utils import make_content_plan, make_planning_phase_result


def _make_ctx(monkeypatch, *, job_id="job-1", **ctx_overrides):
    """Build a ``PipelineContext`` wired to run planning and skip straight to
    outline approval, so tests can focus on the story-elicitation block.

    Preconditions:
        - ``ctx_overrides`` keys are valid ``PipelineContext`` field names.
    Postconditions:
        - Returns a ``PipelineContext`` whose ``run_planning_stage`` call will
          reach and complete the outline-approval loop on its first iteration
          (auto-approved), so only the story-elicitation block under test
          drives ``covered_sections``.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.agent_implementations.pipeline.context import PipelineContext
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from agents.blogging.shared.content_profile import resolve_length_policy

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="Section A", coverage_description="a", order=0),
            ContentPlanSection(title="Section B", coverage_description="b", order=1),
        ],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
    )
    ppr = make_planning_phase_result(
        plan, planning_iterations_used=1, planning_wall_ms_total=10.0, plan_critic_report=None
    )
    monkeypatch.setattr(v2, "run_planning", lambda *_a, **_kw: ppr)

    from agents.blogging.agent_implementations.pipeline import planning_stage

    monkeypatch.setattr(planning_stage, "_wait_for_hitl", lambda *_a, **_kw: False)

    import agents.blogging.shared.blog_job_store as blog_job_store

    monkeypatch.setattr(blog_job_store, "request_draft_feedback", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "is_waiting_for_draft_feedback", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        blog_job_store, "get_user_draft_feedback", lambda *_a, **_kw: {"approved": True}
    )
    monkeypatch.setattr(blog_job_store, "get_blog_job", lambda *_a, **_kw: {"status": "running"})
    monkeypatch.setattr(blog_job_store, "update_blog_job", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "add_story_agent_message", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "complete_story_elicitation", lambda *_a, **_kw: None)

    defaults = dict(
        brief=ResearchBriefInput(brief="A post about testing"),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(),
        series_context=None,
        job_id=job_id,
        job_updater=(lambda **_kw: None) if job_id else None,
        draft_editor_iterations=1,
        max_rewrite_iterations=1,
        run_gates=False,
    )
    defaults.update(ctx_overrides)
    return PipelineContext(**defaults)


def _stub_find_story_gaps(monkeypatch, gaps_or_exc):
    """Patch ``GhostWriterElicitationAgent.find_story_gaps`` to return/raise ``gaps_or_exc``."""
    from agents.blogging import ghost_writer_agent

    def _find_story_gaps(self, _plan):
        if isinstance(gaps_or_exc, BaseException):
            raise gaps_or_exc
        return gaps_or_exc

    monkeypatch.setattr(
        ghost_writer_agent.GhostWriterElicitationAgent, "find_story_gaps", _find_story_gaps
    )


def _stub_conduct_interview(monkeypatch, narratives_by_section: dict):
    """Patch ``conduct_interview`` to return ``narratives_by_section[gap.section_title]``."""
    from agents.blogging import ghost_writer_agent

    def _conduct_interview(self, *, gap, **_kw):
        return SimpleNamespace(narrative=narratives_by_section.get(gap.section_title), gap=gap)

    monkeypatch.setattr(
        ghost_writer_agent.GhostWriterElicitationAgent, "conduct_interview", _conduct_interview
    )


def _stub_find_relevant_stories(monkeypatch, results_or_exc):
    """Patch ``story_bank.find_relevant_stories`` to return/raise ``results_or_exc``."""
    from agents.blogging.shared import story_bank

    def _find_relevant_stories(*_a, **_kw):
        if isinstance(results_or_exc, BaseException):
            raise results_or_exc
        return results_or_exc

    monkeypatch.setattr(story_bank, "find_relevant_stories", _find_relevant_stories)


def _story_gaps(*section_titles: str) -> list:
    from agents.blogging.ghost_writer_agent.models import StoryGap

    return [
        StoryGap(
            section_title=title,
            section_context=f"Context for {title}",
            seed_question=f"Tell me about {title}?",
        )
        for title in section_titles
    ]


def test_interview_narratives_populate_covered_sections(monkeypatch) -> None:
    """Every section with a non-empty fresh-interview narrative is recorded."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage
    from agents.blogging.shared import story_bank

    _stub_find_story_gaps(monkeypatch, _story_gaps("Section A", "Section B"))
    _stub_conduct_interview(
        monkeypatch, {"Section A": "A narrative about A.", "Section B": "A narrative about B."}
    )
    _stub_find_relevant_stories(monkeypatch, [])
    monkeypatch.setattr(story_bank, "save_story", lambda **_kw: None)

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == {"Section A", "Section B"}


def test_whitespace_only_narrative_contributes_no_section(monkeypatch) -> None:
    """A blank/whitespace-only narrative must not add its section, per the
    existing non-empty guard the interview loop already applies."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage
    from agents.blogging.shared import story_bank

    _stub_find_story_gaps(monkeypatch, _story_gaps("Section A", "Section B"))
    _stub_conduct_interview(monkeypatch, {"Section A": "A real narrative.", "Section B": "   "})
    _stub_find_relevant_stories(monkeypatch, [])
    monkeypatch.setattr(story_bank, "save_story", lambda **_kw: None)

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == {"Section A"}


def test_bank_hits_populate_covered_sections(monkeypatch) -> None:
    """A section satisfied only by a story-bank hit (no fresh interview) is included."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    _stub_find_story_gaps(monkeypatch, [])
    _stub_find_relevant_stories(
        monkeypatch,
        [
            {"section_title": "Section B", "narrative": "A banked narrative about B."},
        ],
    )

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == {"Section B"}
    assert ctx.elicited_stories_text is not None
    assert "Section B" in ctx.elicited_stories_text


def test_covered_sections_deduplicated_across_sources(monkeypatch) -> None:
    """The same section satisfied by both an interview and a bank hit appears once."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage
    from agents.blogging.shared import story_bank

    _stub_find_story_gaps(monkeypatch, _story_gaps("Section A"))
    _stub_conduct_interview(monkeypatch, {"Section A": "Fresh narrative about A."})
    _stub_find_relevant_stories(
        monkeypatch,
        [{"section_title": "Section A", "narrative": "A different banked narrative about A."}],
    )
    monkeypatch.setattr(story_bank, "save_story", lambda **_kw: None)

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == {"Section A"}


def test_covered_sections_empty_when_nothing_elicited(monkeypatch) -> None:
    """No story gaps and no bank hits reproduce today's behavior exactly."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    _stub_find_story_gaps(monkeypatch, [])
    _stub_find_relevant_stories(monkeypatch, [])

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == set()
    assert ctx.elicited_stories_text is None
    assert ctx.status == "PASS"
    assert ctx.plan is not None


def test_covered_sections_empty_when_elicitation_skipped_without_job_id(monkeypatch) -> None:
    """Without a job_id/job_updater, elicitation is skipped entirely -- the set
    must still come back as an empty ``set[str]``, never undefined or None."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    _stub_find_relevant_stories(monkeypatch, [])

    # job_id=None short-circuits both the interview block and the outline-approval
    # block, so run_planning_stage returns immediately after story-bank retrieval.
    ctx = _make_ctx(monkeypatch, job_id=None)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == set()


def test_find_story_gaps_failure_leaves_covered_sections_empty(monkeypatch) -> None:
    """A non-cancellation failure in the interview block is swallowed (existing
    best-effort behavior) and leaves ``covered_sections`` empty, not raised."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    _stub_find_story_gaps(monkeypatch, RuntimeError("ghost writer boom"))
    _stub_find_relevant_stories(monkeypatch, [])

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == set()
    assert ctx.status == "PASS"


def test_find_relevant_stories_failure_leaves_covered_sections_empty(monkeypatch) -> None:
    """A non-cancellation failure in the story-bank retrieval block is swallowed
    and leaves ``covered_sections`` empty, not raised."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    _stub_find_story_gaps(monkeypatch, [])
    _stub_find_relevant_stories(monkeypatch, RuntimeError("story bank unavailable"))

    ctx = _make_ctx(monkeypatch)
    assert run_planning_stage(ctx) is None

    assert ctx.covered_sections == set()
    assert ctx.status == "PASS"


def test_cancelled_error_from_find_story_gaps_propagates(monkeypatch) -> None:
    """A Temporal-native cancellation from the interview block is never swallowed."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage
    from temporalio.exceptions import CancelledError

    _stub_find_story_gaps(monkeypatch, CancelledError("cancelled"))
    _stub_find_relevant_stories(monkeypatch, [])

    ctx = _make_ctx(monkeypatch)
    with pytest.raises(CancelledError):
        run_planning_stage(ctx)


def test_cancelled_error_from_find_relevant_stories_propagates(monkeypatch) -> None:
    """A Temporal-native cancellation from the story-bank retrieval block is
    never swallowed -- regression guard for the pre-existing bare
    ``except Exception`` there, which also catches ``CancelledError`` (a
    subclass of ``Exception`` in ``temporalio``) unless explicitly re-raised
    first."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage
    from temporalio.exceptions import CancelledError

    _stub_find_story_gaps(monkeypatch, [])
    _stub_find_relevant_stories(monkeypatch, CancelledError("cancelled"))

    ctx = _make_ctx(monkeypatch)
    with pytest.raises(CancelledError):
        run_planning_stage(ctx)
