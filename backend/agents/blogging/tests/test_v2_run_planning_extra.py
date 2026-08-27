"""Direct tests for ``blog_writing_process_v2.run_planning`` and its helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


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
    # No research stage runs in the v2 pipeline today, so extract_allowed_claims is
    # called with an empty reference list and the brief as topic.
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
