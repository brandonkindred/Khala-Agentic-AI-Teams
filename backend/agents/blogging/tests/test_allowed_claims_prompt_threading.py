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
    # Matches the brief used by the end-to-end pipeline tests below, whose
    # loader now rejects an allowed_claims.json artifact unless its "topic"
    # matches the current run's brief (guards against a stale artifact left
    # in a reused work_dir by an earlier, unrelated brief).
    "topic": "Topic about AI",
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


def test_render_allowed_claims_section_present_but_empty_is_restrictive() -> None:
    """A dict artifact with no usable claims is distinct from no artifact at all:
    it must render a restrictive "make no claims" instruction, not "" (which the
    writer's own prompt treats as "no allowed-claims list was checked, write
    normally")."""
    from agents.blogging.blog_writer_agent.agent import (
        _NO_ALLOWED_CLAIMS_SECTION,
        _render_allowed_claims_section,
    )

    for allowed_claims in (
        {"topic": "x", "claims": []},
        {"topic": "x"},
        {"topic": "x", "claims": "not a list"},
    ):
        section = _render_allowed_claims_section(allowed_claims)
        assert section == _NO_ALLOWED_CLAIMS_SECTION
        assert "ALLOWED CLAIMS" in section
        assert "none available" in section
        assert "Do not make any factual or statistical claims" in section


def test_render_allowed_claims_section_populated() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    section = _render_allowed_claims_section(SAMPLE_ALLOWED_CLAIMS)
    assert "ALLOWED CLAIMS" in section
    assert "[CLAIM:id]" in section
    assert "- [c1] 80% of teams ship weekly." in section
    assert "- [c2] The median deploy takes 4 minutes." in section
    # Preservation guidance for rewrite/self-review contexts lives inside this
    # same block (not layered on by a separate caller-side wrapper), so it
    # never contradicts the restrictive no-claims block's "no tags at all".
    assert "preserve any existing" in section


def test_render_allowed_claims_section_never_uses_em_or_en_dash() -> None:
    """The writer's own prompt tells the model "no em dashes or en dashes" --
    this section (both the populated and restrictive renderings) must not use
    the very characters it later forbids the model from using."""
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    populated = _render_allowed_claims_section(SAMPLE_ALLOWED_CLAIMS)
    restrictive = _render_allowed_claims_section({"claims": []})
    for section in (populated, restrictive):
        assert "—" not in section  # em dash
        assert "–" not in section  # en dash


def test_render_allowed_claims_section_restrictive_and_populated_do_not_conflict() -> None:
    """The restrictive (no-claims) and populated blocks give non-contradictory
    instructions when naively concatenated by a caller — regression test for a
    prior bug where a caller-added "preserve all tags" wrapper contradicted the
    restrictive block's "no [CLAIM:id] tag should appear anywhere"."""
    from agents.blogging.blog_writer_agent.agent import (
        _NO_ALLOWED_CLAIMS_SECTION,
        _render_allowed_claims_section,
    )

    restrictive = _render_allowed_claims_section({"claims": []})
    assert restrictive == _NO_ALLOWED_CLAIMS_SECTION
    assert "No [CLAIM:id] tag should appear" in restrictive
    assert "preserve" not in restrictive.lower()


def test_render_allowed_claims_section_skips_malformed_entries() -> None:
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section

    section = _render_allowed_claims_section(
        {"claims": [{"id": "c1", "text": "Valid."}, {"id": "", "text": "No id."}, "not a dict"]}
    )
    assert "- [c1] Valid." in section
    assert "No id." not in section


def test_render_allowed_claims_section_all_malformed_is_restrictive() -> None:
    from agents.blogging.blog_writer_agent.agent import (
        _NO_ALLOWED_CLAIMS_SECTION,
        _render_allowed_claims_section,
    )

    section = _render_allowed_claims_section(
        {"claims": [{"id": "", "text": "No id."}, {"id": "c1", "text": ""}, "not a dict"]}
    )
    assert section == _NO_ALLOWED_CLAIMS_SECTION


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
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

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
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

    a.run(_writer_input())
    assert "ALLOWED CLAIMS" not in captured["prompt"]


def test_writer_run_threads_allowed_claims_into_self_review(monkeypatch) -> None:
    """run() passes the rendered ALLOWED CLAIMS section into _self_review, so a
    post-generation rewrite pass is told to preserve [CLAIM:id] tags too."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_text",
        lambda self, p, system_prompt="": '{"draft": 0}\n---DRAFT---\n# Out\nBody.',
    )
    captured = {}

    def fake_self_review(self, draft, allowed_claims_section=""):
        captured["allowed_claims_section"] = allowed_claims_section
        return draft

    monkeypatch.setattr(BlogWriterAgent, "_self_review", fake_self_review)

    a.run(_writer_input(allowed_claims=SAMPLE_ALLOWED_CLAIMS))
    assert "ALLOWED CLAIMS" in captured["allowed_claims_section"]
    assert "- [c1] 80% of teams ship weekly." in captured["allowed_claims_section"]


def test_writer_run_suppresses_numeric_requirement_when_claims_restrictive(monkeypatch) -> None:
    """A present-but-empty allowed_claims artifact forbids every factual/statistical
    claim, so the prompt's "at least one specific number" checklist item must not
    also appear -- that would tell the model to satisfy two contradictory mandates
    for a quantitative topic (regression test for a bug where both were present)."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

    a.run(_writer_input(allowed_claims={"topic": "x", "claims": []}))
    assert "at least one specific number" not in captured["prompt"]
    assert "no specific numbers" in captured["prompt"]


def test_writer_run_keeps_numeric_requirement_when_claims_populated(monkeypatch) -> None:
    """A populated allowed_claims list permits factual claims, so the numeric-figure
    checklist item must still be present (unlike the restrictive empty-list case)."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

    a.run(_writer_input(allowed_claims=SAMPLE_ALLOWED_CLAIMS))
    assert "at least one specific number" in captured["prompt"]
    assert "no specific numbers" not in captured["prompt"]


def test_writer_run_keeps_numeric_requirement_when_no_artifact(monkeypatch) -> None:
    """No allowed_claims artifact at all -> the writer's default numeric-figure
    guidance still applies (only the restrictive present-but-empty case suppresses
    it)."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    a = make_writer_agent()
    captured = {"prompt": ""}

    def fake_call(self, prompt, system_prompt=""):
        captured["prompt"] = prompt
        return '{"draft": 0}\n---DRAFT---\n# Out\nBody.'

    monkeypatch.setattr(BlogWriterAgent, "_call_text", fake_call)
    monkeypatch.setattr(
        BlogWriterAgent, "_self_review", lambda self, d, allowed_claims_section="": d
    )

    a.run(_writer_input())
    assert "at least one specific number" in captured["prompt"]


# ---------------------------------------------------------------------------
# _build_revise_all_items_prompt — revision (copy-edit loop / gates rewrite)
# ---------------------------------------------------------------------------


def test_build_revise_all_items_prompt_includes_allowed_claims() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent import revision
    from agents.blogging.blog_writer_agent.agent import _render_allowed_claims_section
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
    prompt = revision.build_revise_all_items_prompt(
        revise_input.draft,
        revise_input.feedback_items,
        "revision plan text",
        a._style_prompt,
        revise_input,
        brand_section=a._brand_section_for_prompt(),
        llm=a._model,
        allowed_claims_section=_render_allowed_claims_section(revise_input.allowed_claims),
    )
    assert "ALLOWED CLAIMS" in prompt
    assert "- [c2] The median deploy takes 4 minutes." in prompt


def test_build_revise_all_items_prompt_omits_allowed_claims_when_absent() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent import revision
    from agents.blogging.blog_writer_agent.models import ReviseWriterInput

    a = make_writer_agent()
    revise_input = ReviseWriterInput(
        draft="# Draft\n\nBody.",
        feedback_items=[
            FeedbackItem(category="style", severity="must_fix", issue="Weak.", suggestion="Fix.")
        ],
        content_plan=_minimal_plan(),
    )
    prompt = revision.build_revise_all_items_prompt(
        revise_input.draft,
        revise_input.feedback_items,
        "revision plan text",
        a._style_prompt,
        revise_input,
        brand_section=a._brand_section_for_prompt(),
        llm=a._model,
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


def test_run_pipeline_ignores_stale_allowed_claims_from_reused_work_dir(
    monkeypatch, tmp_path: Path
) -> None:
    """A work_dir reused across runs may hold an allowed_claims.json artifact
    left by an earlier, unrelated brief. It must not be applied to a new run
    whose brief doesn't match the artifact's "topic"."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared.artifacts import write_artifact

    from ._content_plan_test_utils import make_minimal_planning_phase_result

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: make_minimal_planning_phase_result())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "guidelines text")

    captured: list = []
    monkeypatch.setattr(v2, "BlogWriterAgent", _capturing_stub_writer_class(captured))
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", make_stub_editor_class())

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    write_artifact(
        work_dir,
        "allowed_claims.json",
        {
            "topic": "An earlier, unrelated topic",
            "claims": [{"id": "stale1", "text": "Stale claim."}],
        },
    )

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


# ---------------------------------------------------------------------------
# gates_stage: the fact-check gate must evaluate the draft against the same
# allowed-claims list the writer was given.
# ---------------------------------------------------------------------------


def test_gates_stage_threads_allowed_claims_into_fact_check(monkeypatch, tmp_path: Path) -> None:
    """When run_gates=True, allowed_claims.json from work_dir is passed to the
    fact-check agent so the gate evaluates claims against the same allow-list
    the writer used."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_compliance_agent.models import ComplianceReport
    from agents.blogging.blog_fact_check_agent.models import FactCheckReport
    from agents.blogging.shared.artifacts import write_artifact

    from ._content_plan_test_utils import make_minimal_planning_phase_result
    from .conftest import make_stub_writer_class

    monkeypatch.setattr(v2, "run_planning", lambda *a, **kw: make_minimal_planning_phase_result())
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "guidelines text")
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda *a, **kw: "brand")
    monkeypatch.setattr(v2, "BlogWriterAgent", make_stub_writer_class())
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", make_stub_editor_class())

    class _ValidatorStub:
        status = "PASS"
        checks = []

        def model_dump(self):
            return {"status": "PASS", "checks": []}

    monkeypatch.setattr(v2, "run_validators_from_work_dir", lambda wd, **kw: _ValidatorStub())
    monkeypatch.setattr(
        v2,
        "BlogComplianceAgent",
        type(
            "_Compliance",
            (),
            {
                "__init__": lambda self, *a, **kw: None,
                "run": lambda self, *a, **kw: ComplianceReport(
                    status="PASS", violations=[], required_fixes=[], notes="ok"
                ),
            },
        ),
    )

    captured_kwargs = {}

    class _CapturingFactCheck:
        def __init__(self, *a, **kw):
            pass

        def run(self, draft, **kwargs):
            captured_kwargs.update(kwargs)
            return FactCheckReport(
                claims_status="PASS",
                risk_status="PASS",
                risk_flags=[],
                required_disclaimers=[],
                unverified_claims=[],
                claims=[],
            )

    monkeypatch.setattr(v2, "BlogFactCheckAgent", _CapturingFactCheck)

    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    write_artifact(work_dir, "allowed_claims.json", SAMPLE_ALLOWED_CLAIMS)

    brief = ResearchBriefInput(brief="Topic about AI", audience="devs", max_results=10)
    _, _, status = v2.run_pipeline(
        brief,
        work_dir=work_dir,
        run_gates=True,
        max_rewrite_iterations=1,
        draft_editor_iterations=1,
        llm_client=object(),
    )
    assert status == "PASS"
    assert captured_kwargs.get("allowed_claims") == SAMPLE_ALLOWED_CLAIMS
