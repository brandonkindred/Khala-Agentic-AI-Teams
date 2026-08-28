"""Direct tests for ``blog_writing_process_v2.run_planning`` and its helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _patch_stub_research_agent(monkeypatch, v2) -> None:
    """Patch ``v2.ResearchAgent`` with a no-op stub for tests that don't exercise research."""
    from agents.blogging.blog_research_agent.models import ResearchAgentOutput

    class _StubResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(
                query_plan=[], references=[], compiled_document="stub research"
            )

    monkeypatch.setattr(v2, "ResearchAgent", _StubResearchAgent)


def _make_planning_result(iterations: int = 1, critic_report: dict | None = None):
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan, make_planning_phase_result

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="hook", order=0),
            ContentPlanSection(title="Body", coverage_description="meat", order=1),
            ContentPlanSection(title="Conclusion", coverage_description="wrap", order=2),
        ],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
    )
    return make_planning_phase_result(
        plan,
        planning_iterations_used=iterations,
        planning_wall_ms_total=10.0,
        plan_critic_report=critic_report,
    )


def _patch_planning_collaborators(monkeypatch, v2, ppr, *, critic_agent=None):
    """Patch run_planning's non-claims collaborators shared by the allowed-claims tests."""

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: critic_agent)


def test_run_planning_writes_artifacts_and_returns_result(monkeypatch, tmp_path: Path) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(iterations=2, critic_report={"status": "PASS"})

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: object())

    work_dir = tmp_path / "wd"
    work_dir.mkdir(parents=True, exist_ok=True)

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=work_dir,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    assert result is ppr
    assert (work_dir / "content_plan.json").exists()
    assert (work_dir / "outline.md").exists()
    assert (work_dir / "plan_critic_report.json").exists()


def test_run_planning_runs_research_and_writes_packet(monkeypatch, tmp_path: Path) -> None:
    """ResearchAgent.run() is invoked and its compiled_document persisted as
    research_packet.md, with progress reported via job_updater."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import (
        ResearchAgentOutput,
        ResearchBriefInput,
        ResearchReference,
    )
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result()
    compiled_doc = "# Blog Post Research\n\n## Sources\n\n1. https://example.com\n-- summary\n"
    research_output = ResearchAgentOutput(
        query_plan=[],
        references=[
            ResearchReference(
                url="https://example.com",
                title="Example",
                summary="summary",
                key_points=["point"],
            )
        ],
        compiled_document=compiled_doc,
    )

    planning_prompts: list[str] = []

    class _FakePlanAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, planning_input, **_kw):
            from agents.blogging.shared.content_planning_loop import build_generate_plan_prompt

            planning_prompts.append(build_generate_plan_prompt(planning_input))
            return ppr

    research_calls: list = []

    class _FakeResearchAgent:
        def __init__(self, **kw):
            research_calls.append(("init", kw))

        def run(self, brief_input):
            research_calls.append(("run", brief_input))
            return research_output

    updates: list = []

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakePlanAgent)
    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    work_dir = tmp_path / "wd"
    brief = ResearchBriefInput(
        brief="b", audience="devs", tone_or_purpose="informative", max_results=5
    )

    result = v2.run_planning(
        brief,
        work_dir=work_dir,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=lambda **kw: updates.append(kw),
    )

    assert result is ppr
    # ResearchAgent.run() was called with the pipeline's brief unchanged.
    assert research_calls[0][0] == "init"
    assert research_calls[1] == ("run", brief)
    assert (work_dir / "research_packet.md").read_text(encoding="utf-8") == compiled_doc
    assert compiled_doc.strip() in planning_prompts[0]
    research_updates = [u for u in updates if u.get("phase") == "research"]
    assert len(research_updates) == 2
    assert research_updates[0]["status_text"] == "Researching topic..."
    assert "1 reference" in research_updates[1]["status_text"]
    # research_sources_count is a real job-store field (initialized to 0, surfaced by
    # the status API) that only this completion update can populate.
    assert research_updates[1]["research_sources_count"] == 1

    # Checkpointed under work_dir so a Temporal retry resumes research instead of
    # repeating every search/fetch/summarization call from scratch.
    from agents.blogging.blog_research_agent.agent_cache import AgentCache

    init_kwargs = research_calls[0][1]
    assert isinstance(init_kwargs["cache"], AgentCache)
    assert init_kwargs["cache"].cache_dir == work_dir / ".research_cache"


def test_run_planning_research_zero_references_writes_fallback_packet(
    monkeypatch, tmp_path: Path
) -> None:
    """A research run with no references still writes research_packet.md (using the
    agent's own "no sources found" fallback text) and reports 0 references."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchAgentOutput, ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result()
    compiled_doc = "# Blog Post Research\n\n## Sources\n\n(No web sources found.)\n"

    planning_digests: list[str] = []

    class _FakePlanAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, planning_input, **_kw):
            planning_digests.append(planning_input.research_digest)
            return ppr

    class _FakeResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(query_plan=[], references=[], compiled_document=compiled_doc)

    updates: list = []

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakePlanAgent)
    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    work_dir = tmp_path / "wd"

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=work_dir,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=lambda **kw: updates.append(kw),
    )

    assert result is ppr
    assert (work_dir / "research_packet.md").read_text(encoding="utf-8") == compiled_doc
    assert planning_digests == [""]
    research_updates = [u for u in updates if u.get("phase") == "research"]
    assert "0 reference" in research_updates[1]["status_text"]


def test_run_planning_keeps_academic_only_research_digest(monkeypatch) -> None:
    """Academic papers remain valid evidence when web references are empty."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import (
        AcademicPaper,
        ResearchAgentOutput,
        ResearchBriefInput,
    )
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result()
    compiled_doc = "# Academic Sources\n\nA useful paper."
    planning_digests: list[str] = []

    class _FakePlanAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, planning_input, **_kw):
            planning_digests.append(planning_input.research_digest)
            return ppr

    class _FakeResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(
                query_plan=[],
                references=[],
                academic_papers=[
                    AcademicPaper(
                        title="Useful Paper",
                        url="https://arxiv.org/abs/1234.5678",
                        overview_or_summary="Useful findings.",
                    )
                ],
                compiled_document=compiled_doc,
            )

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakePlanAgent)
    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    assert planning_digests == [compiled_doc]


def test_run_planning_caps_research_digest(monkeypatch) -> None:
    """Oversized research is compacted before it reaches the planning prompt."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import (
        ResearchAgentOutput,
        ResearchBriefInput,
        ResearchReference,
    )
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    import llm_service

    ppr = _make_planning_result()
    oversized_document = "x" * 200_001
    compacted_digest = "bounded digest"
    planning_digests: list[str] = []
    compaction_calls: list[tuple] = []

    class _FakePlanAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, planning_input, **_kw):
            planning_digests.append(planning_input.research_digest)
            return ppr

    class _FakeResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(
                query_plan=[],
                references=[
                    ResearchReference(url="https://example.com", title="Example", summary="Summary")
                ],
                compiled_document=oversized_document,
            )

    def _compact(text, max_chars, llm, label):
        compaction_calls.append((text, max_chars, llm, label))
        return compacted_digest

    llm_client = object()
    monkeypatch.setattr(v2, "BlogWriterAgent", _FakePlanAgent)
    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)
    monkeypatch.setattr(llm_service, "compact_text", _compact)

    v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=None,
        llm_client=llm_client,
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    assert planning_digests == [compacted_digest]
    assert compaction_calls == [(oversized_document, 200_000, llm_client, "research digest")]


def test_run_planning_writes_allowed_claims_with_extracted_claims(
    monkeypatch, tmp_path: Path
) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.allowed_claims import AllowedClaims, ClaimEntry
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(critic_report=None)
    populated = AllowedClaims(
        topic="b",
        claims=[
            ClaimEntry(id="1", text="A verified claim.", citations=["Source 1"], risk_level="low")
        ],
    )
    captured: dict = {}

    def _fake_extract(llm_client, compiled_document, references, topic=""):
        captured["compiled_document"] = compiled_document
        captured["references"] = references
        captured["topic"] = topic
        return populated

    _patch_planning_collaborators(monkeypatch, v2, ppr)
    monkeypatch.setattr(v2, "extract_allowed_claims", _fake_extract)

    work_dir = tmp_path / "wd"
    work_dir.mkdir(parents=True, exist_ok=True)

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=work_dir,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    assert result is ppr
    written = json.loads((work_dir / "allowed_claims.json").read_text())
    assert written == populated.to_dict()
    assert written["claims"][0]["text"] == "A verified claim."
    # extract_allowed_claims isn't yet wired to the research call's output (a
    # separate follow-up), so it's still called with an empty reference list and
    # the brief as topic.
    assert captured["references"] == []
    assert captured["topic"] == "b"
    assert captured["compiled_document"]


def test_run_planning_writes_allowed_claims_with_zero_claims(monkeypatch, tmp_path: Path) -> None:
    """extract_allowed_claims never raises; a zero-claims result still yields a valid artifact."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.allowed_claims import AllowedClaims
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(critic_report=None)

    _patch_planning_collaborators(monkeypatch, v2, ppr)
    monkeypatch.setattr(
        v2, "extract_allowed_claims", lambda *_a, **_kw: AllowedClaims(topic="b", claims=[])
    )

    work_dir = tmp_path / "wd"
    work_dir.mkdir(parents=True, exist_ok=True)

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=work_dir,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    assert result is ppr
    written = json.loads((work_dir / "allowed_claims.json").read_text())
    assert written == {"topic": "b", "claims": []}


def test_run_planning_unwraps_llm_client_model_for_claims_extraction(
    monkeypatch, tmp_path: Path
) -> None:
    """extract_allowed_claims must reach the backing client's complete_json, not the
    Strands LLMClientModel wrapper the pipeline actually passes in production.

    Regression test for the Codex finding on PR #7408: LLMClientModel (returned by
    get_strands_model, what run_pipeline/Temporal activities pass as llm_client) has
    no complete_json method, so passing it unwrapped silently produced empty claims
    in every real run (extract_allowed_claims catches the AttributeError and falls
    back to zero claims). This test does NOT mock extract_allowed_claims — it must
    exercise the real function against a real LLMClientModel to catch this.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    from llm_service import LLMClientModel

    ppr = _make_planning_result(critic_report=None)

    class _FakeBackingClient:
        def complete_json(self, _prompt, **_kw):
            return {
                "claims": [
                    {
                        "id": "1",
                        "text": "Claim from the backing client.",
                        "citations": ["Source 1"],
                        "risk_level": "low",
                    }
                ]
            }

    _patch_planning_collaborators(monkeypatch, v2, ppr)

    work_dir = tmp_path / "wd"
    work_dir.mkdir(parents=True, exist_ok=True)

    v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=work_dir,
        llm_client=LLMClientModel(_FakeBackingClient()),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )

    written = json.loads((work_dir / "allowed_claims.json").read_text())
    assert written["claims"] == [
        {
            "id": "1",
            "text": "Claim from the backing client.",
            "citations": ["Source 1"],
            "risk_level": "low",
        }
    ]


def test_run_planning_research_progress_reraises_cancelled_error(monkeypatch) -> None:
    """A CancelledError from job_updater during the research progress report must
    propagate (never be swallowed), matching _make_update's own CancelledError
    handling for planning's own progress reports."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchAgentOutput, ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy
    from temporalio.exceptions import CancelledError

    class _FakeResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(query_plan=[], references=[], compiled_document="doc")

    def cancelling_updater(**_kw):
        raise CancelledError("job cancelled")

    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)

    with pytest.raises(CancelledError):
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
            series_context=None,
            job_updater=cancelling_updater,
        )


def test_run_planning_research_failure_wraps_as_research_error(monkeypatch) -> None:
    """A non-transient ResearchAgent.run() failure is wrapped as ResearchError
    (phase="research") rather than left to surface under the "planning" phase that
    Temporal/thread-mode failure tracking otherwise defaults to."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.errors import ResearchError

    class _FailingResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            raise RuntimeError("search backend unreachable")

    monkeypatch.setattr(v2, "ResearchAgent", _FailingResearchAgent)

    with pytest.raises(ResearchError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value.phase == "research"
    assert "Research failed" in str(exc.value)


def test_run_planning_research_artifact_write_failure_wraps_as_research_error(
    monkeypatch, tmp_path: Path
) -> None:
    """A write_artifact() failure for research_packet.md is attributed to research
    too (not left to surface unattributed / under "planning"), since it happens
    inside the same try/except as the ResearchAgent.run() call."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    import agents.blogging.agent_implementations.pipeline._common as common_mod
    from agents.blogging.blog_research_agent.models import ResearchAgentOutput, ResearchBriefInput
    from agents.blogging.shared.errors import ResearchError

    class _FakeResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            return ResearchAgentOutput(query_plan=[], references=[], compiled_document="doc")

    def boom_write_artifact(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)
    # write_artifact is a direct top-level import in _common.py (not a deferred
    # v2-shim lookup), so it must be patched on the defining module.
    monkeypatch.setattr(common_mod, "write_artifact", boom_write_artifact)

    with pytest.raises(ResearchError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=tmp_path / "wd",
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value.phase == "research"


def test_run_planning_research_cache_setup_failure_wraps_as_research_error(
    monkeypatch, tmp_path: Path
) -> None:
    """An AgentCache construction failure (e.g. work_dir/.research_cache cannot be
    created) is attributed to research too, since cache setup now happens inside the
    same try/except as the ResearchAgent.run() call and the artifact write."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    import agents.blogging.agent_implementations.pipeline._common as common_mod
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.errors import ResearchError

    class _BoomAgentCache:
        def __init__(self, **_kw):
            raise OSError("read-only filesystem")

    monkeypatch.setattr(common_mod, "AgentCache", _BoomAgentCache)

    with pytest.raises(ResearchError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=tmp_path / "wd",
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value.phase == "research"


@pytest.mark.parametrize("err_cls_name", ["LLMRateLimitError", "LLMTemporaryError"])
def test_run_planning_research_reraises_transient_llm_errors(monkeypatch, err_cls_name) -> None:
    """Transient LLM errors from research must stay unwrapped for Temporal retry,
    same as the planning call's own transient-error handling."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if err_cls_name == "LLMRateLimitError" else LLMTemporaryError
    cause = err_cls("transient outage")

    class _FailingResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            raise cause

    monkeypatch.setattr(v2, "ResearchAgent", _FailingResearchAgent)

    with pytest.raises(err_cls) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value is cause


@pytest.mark.parametrize("err_cls_name", ["LLMRateLimitError", "LLMTemporaryError"])
def test_run_planning_research_unwraps_strands_transient_errors(monkeypatch, err_cls_name) -> None:
    """A transient LLM error that Strands wraps in EventLoopException (as a live
    ResearchAgent._call_json() call would raise it) must still be recognized as
    transient and re-raised unwrapped, not turned into a terminal ResearchError.

    Unlike the planner's LLM calls (which go through run_json_gate/
    call_json_with_retry and unwrap EventLoopException themselves),
    ResearchAgent._call_json() calls Strands directly, so the unwrap has to happen
    at this boundary instead.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if err_cls_name == "LLMRateLimitError" else LLMTemporaryError
    cause = err_cls("transient outage")
    wrapped = EventLoopException(cause)

    class _FailingResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            raise wrapped

    monkeypatch.setattr(v2, "ResearchAgent", _FailingResearchAgent)

    with pytest.raises(err_cls) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value is cause


def test_run_planning_research_reraises_blogging_error(monkeypatch) -> None:
    """A BloggingError raised directly by research (e.g. a ResearchError from a
    sub-step) propagates unwrapped rather than being double-wrapped."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.errors import ResearchError

    cause = ResearchError("no sources reachable", sources_found=0)

    class _FailingResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            raise cause

    monkeypatch.setattr(v2, "ResearchAgent", _FailingResearchAgent)

    with pytest.raises(ResearchError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value is cause


def test_run_planning_research_reraises_cancelled_error(monkeypatch) -> None:
    """A CancelledError raised directly by ResearchAgent.run() (a Temporal runtime
    cancellation, not a job_updater failure) propagates unwrapped rather than being
    turned into a terminal ResearchError."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from temporalio.exceptions import CancelledError

    cause = CancelledError("job cancelled")

    class _FailingResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            raise cause

    monkeypatch.setattr(v2, "ResearchAgent", _FailingResearchAgent)

    with pytest.raises(CancelledError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value is cause


def test_run_planning_research_unwraps_strands_cancelled_error(monkeypatch) -> None:
    """A Temporal cancellation that Strands wraps in EventLoopException (as a live
    ResearchAgent._call_json() call would raise it) must still be recognized as a
    cancellation and re-raised unwrapped, not turned into a terminal ResearchError.

    _is_external_cancellation walks __cause__/__context__, which can't see into
    EventLoopException.original_exception, so the research except-block must test
    the *unwrapped* cause for cancellation (mirroring the transient-LLM-error unwrap
    a few lines above it) and re-raise that cause, not the wrapper.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from strands.types.exceptions import EventLoopException
    from temporalio.exceptions import CancelledError

    cause = CancelledError("job cancelled")
    wrapped = EventLoopException(cause)

    class _FailingResearchAgent:
        def __init__(self, **_kw):
            pass

        def run(self, _brief):
            raise wrapped

    monkeypatch.setattr(v2, "ResearchAgent", _FailingResearchAgent)

    with pytest.raises(CancelledError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=None,
            series_context=None,
            job_updater=None,
        )
    assert exc.value is cause


def test_run_planning_research_agent_gets_no_cache_without_work_dir(monkeypatch) -> None:
    """When work_dir is None there's nothing to checkpoint against, so ResearchAgent
    is constructed with cache=None rather than falling back to a shared/default dir."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchAgentOutput, ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result()
    research_calls: list = []

    class _FakePlanAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    class _FakeResearchAgent:
        def __init__(self, **kw):
            research_calls.append(kw)

        def run(self, _brief):
            return ResearchAgentOutput(query_plan=[], references=[], compiled_document="doc")

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakePlanAgent)
    monkeypatch.setattr(v2, "ResearchAgent", _FakeResearchAgent)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )
    assert research_calls[0]["cache"] is None


def test_run_planning_without_work_dir_or_critic_report(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(critic_report=None)

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "brand")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "style")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=None,
    )
    assert result is ppr


def test_run_planning_job_updater_swallows_errors(monkeypatch, tmp_path: Path) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    ppr = _make_planning_result(critic_report=None)

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            return ppr

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    def boom(**kw):
        raise RuntimeError("updater down")

    result = v2.run_planning(
        ResearchBriefInput(brief="b", max_results=5),
        work_dir=tmp_path / "wd2",
        llm_client=object(),
        length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
        series_context=None,
        job_updater=boom,
    )
    assert result is ppr


def test_run_planning_propagates_blogging_error(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy
    from agents.blogging.shared.errors import PlanningError

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            raise PlanningError("planner died", failure_reason="X")

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    with pytest.raises(PlanningError):
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
            series_context=None,
            job_updater=None,
        )


def test_run_planning_wraps_unknown_exception(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy
    from agents.blogging.shared.errors import PlanningError

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            raise RuntimeError("transport failed")

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    with pytest.raises(PlanningError) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
            series_context=None,
            job_updater=None,
        )
    assert "Planning failed" in str(exc.value)


@pytest.mark.parametrize("err_cls_name", ["LLMRateLimitError", "LLMTemporaryError"])
def test_run_planning_reraises_transient_llm_errors(monkeypatch, err_cls_name) -> None:
    """Transient LLM errors must stay unwrapped for Temporal stage retry."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import ContentProfile, resolve_length_policy

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if err_cls_name == "LLMRateLimitError" else LLMTemporaryError
    cause = err_cls("transient outage")

    class _FakeAgent:
        def __init__(self, **_kw):
            pass

        def plan_content(self, *_a, **_kw):
            raise cause

    monkeypatch.setattr(v2, "BlogWriterAgent", _FakeAgent)
    _patch_stub_research_agent(monkeypatch, v2)
    monkeypatch.setattr(v2, "load_brand_spec_prompt", lambda _p: "")
    monkeypatch.setattr(v2, "load_style_file", lambda _p: "")
    monkeypatch.setattr(v2, "build_plan_critic_agent", lambda _llm: None)

    with pytest.raises(err_cls) as exc:
        v2.run_planning(
            ResearchBriefInput(brief="b", max_results=5),
            work_dir=None,
            llm_client=object(),
            length_policy=resolve_length_policy(content_profile=ContentProfile.standard_article),
            series_context=None,
            job_updater=None,
        )
    assert exc.value is cause


def test_extract_plan_keywords_returns_filtered_unique() -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="observability essentials",
        narrative_flow="x",
        sections=[
            ContentPlanSection(title="Tracing and Metrics", coverage_description="x", order=0),
            ContentPlanSection(title="Cost Attribution", coverage_description="x", order=1),
        ],
        title_candidates=[TitleCandidate(title="t", probability_of_success=0.5)],
    )
    kws = v2._extract_plan_keywords(plan)
    assert "and" not in kws
    assert "observability" in kws
    assert "tracing" in kws
    assert "metrics" in kws


def test_planning_llm_client_overrides_when_model_set(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    from llm_service.factory import FailoverLLMClient

    monkeypatch.setattr(v2, "planning_model_override", lambda: "override-model")
    # In production the blog base client is always a FailoverLLMClient; the override
    # is applied per call to Ollama candidates so multi-provider failover is preserved.
    base = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)
    out = v2.planning_llm_client(base)
    assert isinstance(out, FailoverLLMClient)
    assert out is not base
    assert out._model_override == "override-model"
    # The same load/build closures are reused, so agent attribution and the reasoning
    # hook captured at get_client time carry across unchanged.
    assert out._build is base._build and out._load_candidates is base._load_candidates


def test_planning_llm_client_no_override_returns_base(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "planning_model_override", lambda: "")
    sentinel = object()
    assert v2.planning_llm_client(sentinel) is sentinel


def test_plan_critic_llm_client_overrides_when_model_set(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    from llm_service.factory import FailoverLLMClient

    monkeypatch.setattr(v2, "plan_critic_model_override", lambda: "critic-model")
    base = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)
    out = v2.plan_critic_llm_client(base)
    assert isinstance(out, FailoverLLMClient)
    assert out is not base
    assert out._model_override == "critic-model"
    # The same load/build closures are reused, so attribution + reasoning hook carry across.
    assert out._build is base._build and out._load_candidates is base._load_candidates


def test_plan_critic_llm_client_no_override_returns_base(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "plan_critic_model_override", lambda: "")
    sentinel = object()
    assert v2.plan_critic_llm_client(sentinel) is sentinel


def test_planning_llm_client_override_reaches_strands_backing(monkeypatch) -> None:
    """End-to-end: the pipeline passes a Strands LLMClientModel, so the override must
    reach the backing failover client (rebuilding the model) rather than no-op."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    from llm_service import LLMClientModel
    from llm_service.factory import FailoverLLMClient

    monkeypatch.setattr(v2, "planning_model_override", lambda: "override-model")
    backing = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)
    model = LLMClientModel(backing, agent_key="blog", response_format="text")
    out = v2.planning_llm_client(model)
    assert isinstance(out, LLMClientModel)
    assert out is not model
    assert isinstance(out.client, FailoverLLMClient)
    assert out.client._model_override == "override-model"
    # The rebuilt model carries the original config (e.g. response format).
    assert out.get_config()["response_format"] == "text"


def test_plan_critic_llm_client_override_reaches_strands_backing(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    from llm_service import LLMClientModel
    from llm_service.factory import FailoverLLMClient

    monkeypatch.setattr(v2, "plan_critic_model_override", lambda: "critic-model")
    backing = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)
    out = v2.plan_critic_llm_client(LLMClientModel(backing, agent_key="blog"))
    assert isinstance(out, LLMClientModel)
    assert out.client._model_override == "critic-model"


def test_planning_llm_client_strands_dummy_backing_unchanged(monkeypatch) -> None:
    """A Strands model over a Dummy backing has no failover client to pin, so the
    same model instance is returned (no needless rebuild)."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    from llm_service import DummyLLMClient, LLMClientModel

    monkeypatch.setattr(v2, "planning_model_override", lambda: "override-model")
    model = LLMClientModel(DummyLLMClient(), agent_key="blog")
    assert v2.planning_llm_client(model) is model


def test_build_plan_critic_agent_disabled_returns_none(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "plan_critic_enabled", lambda: False)
    assert v2.build_plan_critic_agent(object()) is None


def test_build_plan_critic_agent_enabled_returns_instance(monkeypatch) -> None:
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "plan_critic_enabled", lambda: True)
    monkeypatch.setattr(v2, "plan_critic_llm_client", lambda b: b)
    agent = v2.build_plan_critic_agent(object())
    assert agent is not None
