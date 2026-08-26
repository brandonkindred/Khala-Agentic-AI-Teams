"""Tests for threading allowed_claims.json into the writer's prompt context.

Covers ``_render_allowed_claims_section`` directly, its wiring into
``BlogWriterAgent.run()`` (initial draft) and ``_build_revise_all_items_prompt``
(revision, used by both the copy-edit loop and the gates rewrite loop), and the
end-to-end pipeline path that loads ``allowed_claims.json`` from ``work_dir``.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import make_stub_editor_class, make_writer_agent


def _writer_input(**overrides):
    from agents.blogging.blog_writer_agent.models import WriterInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    kwargs = {
        "content_plan": plan,
        "audience": "devs",
        "tone_or_purpose": "inform",
    }
    kwargs.update(overrides)
    return WriterInput(**kwargs)


def _minimal_plan():
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    return make_content_plan(
        overarching_topic="Test topic",
        narrative_flow="Intro, main, wrap.",
        sections=[ContentPlanSection(title="Intro", coverage_description="Hook", order=0)],
        title_candidates=[TitleCandidate(title="T1", probability_of_success=0.5)],
    )


SAMPLE_ALLOWED_CLAIMS = {
    "topic": "Test topic",
    "claims": [
        {"id": "c1", "text": "80% of teams ship weekly.", "citations": ["Source 1"]},
        {"id": "c2", "text": "The median deploy takes 4 minutes.", "citations": []},
    ],
}


# ---------------------------------------------------------------------------
# _render_allowed_claims_section
# ---------------------------------------------------------------------------


def test_render_allowed_claims_section_none_returns_empty() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    assert _render_allowed_claims_section(None) == ""


def test_render_allowed_claims_section_non_dict_returns_empty() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    assert _render_allowed_claims_section("not a dict") == ""
    assert _render_allowed_claims_section([1, 2, 3]) == ""


def test_render_allowed_claims_section_empty_claims_returns_empty() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    assert _render_allowed_claims_section({"topic": "x", "claims": []}) == ""
    assert _render_allowed_claims_section({"topic": "x"}) == ""


def test_render_allowed_claims_section_populated() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    section = _render_allowed_claims_section(SAMPLE_ALLOWED_CLAIMS)
    assert "ALLOWED CLAIMS" in section
    assert "[CLAIM:id]" in section
    assert "- [c1] 80% of teams ship weekly." in section
    assert "- [c2] The median deploy takes 4 minutes." in section


def test_render_allowed_claims_section_skips_malformed_entries() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    section = _render_allowed_claims_section(
        {"claims": [{"id": "c1", "text": "Valid."}, {"id": "", "text": "No id."}, "not a dict"]}
    )
    assert "- [c1] Valid." in section
    assert "No id." not in section


# ---------------------------------------------------------------------------
# BlogWriterAgent.run() — initial draft
# ---------------------------------------------------------------------------


def test_writer_run_includes_allowed_claims_when_provided(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody with [CLAIM:c1] tag.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d: d)

    out = a.run(_writer_input(allowed_claims=SAMPLE_ALLOWED_CLAIMS))
    assert "ALLOWED CLAIMS" in captured["prompt"]
    assert "- [c1] 80% of teams ship weekly." in captured["prompt"]
    assert "[CLAIM:c1]" in out.draft


def test_writer_run_omits_allowed_claims_section_when_absent(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(BlogWriterAgent, "_self_review", lambda self, d: d)

    a.run(_writer_input())
    assert "ALLOWED CLAIMS" not in captured["prompt"]


# ---------------------------------------------------------------------------
# _build_revise_all_items_prompt — revision (copy-edit loop / gates rewrite)
# ---------------------------------------------------------------------------


def test_build_revise_all_items_prompt_includes_allowed_claims() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput

    a = make_writer_agent()
    revise_input = ReviseWriterInput(
        draft="# Draft\n\nBody.",
        feedback_items=[
            FeedbackItem(
                category="style",
                severity="must_fix",
                issue="Weak opening.",
                suggestion="Add a hook.",
            )
        ],
        content_plan=_minimal_plan(),
        allowed_claims=SAMPLE_ALLOWED_CLAIMS,
    )
    prompt = a._build_revise_all_items_prompt(
        revise_input.draft,
        revise_input.feedback_items,
        "revision plan text",
        a._style_prompt,
        revise_input,
    )
    assert "ALLOWED CLAIMS" in prompt
    assert "- [c2] The median deploy takes 4 minutes." in prompt


def test_build_revise_all_items_prompt_omits_allowed_claims_when_absent() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput

    a = make_writer_agent()
    revise_input = ReviseWriterInput(
        draft="# Draft\n\nBody.",
        feedback_items=[
            FeedbackItem(category="style", severity="must_fix", issue="Weak.", suggestion="Fix.")
        ],
        content_plan=_minimal_plan(),
    )
    prompt = a._build_revise_all_items_prompt(
        revise_input.draft,
        revise_input.feedback_items,
        "revision plan text",
        a._style_prompt,
        revise_input,
    )
    assert "ALLOWED CLAIMS" not in prompt


# ---------------------------------------------------------------------------
# revise_from_user_feedback — direct user/editor feedback revision (uncertainty
# answers, interactive review, escalation), the free-form-kwargs sibling to
# _build_revise_all_items_prompt.
# ---------------------------------------------------------------------------


def test_revise_from_user_feedback_includes_allowed_claims(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody with [CLAIM:c1] tag.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)

    out = a.revise_from_user_feedback(
        draft="# Draft\n\nBody.",
        user_feedback="Tighten the intro.",
        content_plan_text="- Intro\n- Body",
        allowed_claims=SAMPLE_ALLOWED_CLAIMS,
    )
    assert "ALLOWED CLAIMS" in captured["prompt"]
    assert "- [c1] 80% of teams ship weekly." in captured["prompt"]
    assert "[CLAIM:c1]" in out.draft


def test_revise_from_user_feedback_omits_allowed_claims_when_absent(monkeypatch) -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)

    a.revise_from_user_feedback(
        draft="# Draft\n\nBody.",
        user_feedback="Tighten the intro.",
        content_plan_text="- Intro\n- Body",
    )
    assert "ALLOWED CLAIMS" not in captured["prompt"]


# ---------------------------------------------------------------------------
# End-to-end: run_pipeline loads allowed_claims.json from work_dir and threads
# it into both the initial WriterInput and the copy-edit-loop ReviseWriterInput.
# ---------------------------------------------------------------------------


def _capturing_stub_writer_class(captured_inputs: list) -> type:
    from agents.blogging.blog_writer_agent.models import WriterOutput

    class _CapturingStubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, draft_input, *a, **kw):
            captured_inputs.append(("run", draft_input))
            return WriterOutput(draft="# Draft\n\nBody with [CLAIM:c1] tag.")

        def revise(self, revise_input, *a, **kw):
            captured_inputs.append(("revise", revise_input))
            return WriterOutput(draft="# Revised\n\nBody with [CLAIM:c1] tag.")

        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft="# Revised\n\nBody.")

        def identify_uncertainty_questions(self, *a, **kw):
            return []

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return ""

    return _CapturingStubWriter


def make_stub_editor_class_that_requests_one_revision() -> type:
    """A BlogCopyEditorAgent stub that rejects the first draft, approves the second."""
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorOutput, FeedbackItem

    class _StubEditor:
        def __init__(self, *a, **kw):
            self._calls = 0

        def run(self, *a, **kw):
            self._calls += 1
            return CopyEditorOutput(
                approved=self._calls > 1,
                summary="revise" if self._calls == 1 else "ok",
                feedback_items=[]
                if self._calls > 1
                else [
                    FeedbackItem(
                        category="style",
                        severity="must_fix",
                        issue="Needs work.",
                        suggestion="Fix it.",
                    )
                ],
            )

    return _StubEditor


def test_run_pipeline_threads_allowed_claims_from_work_dir(monkeypatch, tmp_path: Path) -> None:
    """allowed_claims.json in work_dir reaches both the initial draft and the
    copy-edit-loop revision WriterInput/ReviseWriterInput objects.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared.artifacts import write_artifact

    from ._content_plan_test_utils import make_minimal_planning_phase_result

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: make_minimal_planning_phase_result())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "guidelines text")

    captured: list = []
    monkeypatch.setattr(v2, "BlogWriterAgent", _capturing_stub_writer_class(captured))
    monkeypatch.setattr(
        v2, "BlogCopyEditorAgent", make_stub_editor_class_that_requests_one_revision()
    )

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    write_artifact(work_dir, "allowed_claims.json", SAMPLE_ALLOWED_CLAIMS)

    brief = ResearchBriefInput(brief="Topic about AI", audience="devs", max_results=10)
    _, draft_result, status = v2.run_pipeline(
        brief,
        work_dir=work_dir,
        run_gates=False,
        draft_editor_iterations=2,
        llm_client=object(),
    )

    assert status == "PASS"
    kinds = [k for k, _ in captured]
    assert "run" in kinds
    for kind, draft_input in captured:
        assert draft_input.allowed_claims == SAMPLE_ALLOWED_CLAIMS, (
            f"{kind} call did not receive allowed_claims"
        )
    assert "[CLAIM:c1]" in draft_result.draft


def test_run_pipeline_no_allowed_claims_artifact_is_noop(monkeypatch, tmp_path: Path) -> None:
    """No allowed_claims.json present -> WriterInput.allowed_claims stays None."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    from ._content_plan_test_utils import make_minimal_planning_phase_result

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: make_minimal_planning_phase_result())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "guidelines text")

    captured: list = []
    monkeypatch.setattr(v2, "BlogWriterAgent", _capturing_stub_writer_class(captured))
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", make_stub_editor_class())

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    work_dir = tmp_path / "wd"
    brief = ResearchBriefInput(brief="Topic about AI", audience="devs", max_results=10)
    _, _, status = v2.run_pipeline(
        brief,
        work_dir=work_dir,
        run_gates=False,
        draft_editor_iterations=1,
        llm_client=object(),
    )
    assert status == "PASS"
    assert captured
    for _kind, draft_input in captured:
        assert draft_input.allowed_claims is None
