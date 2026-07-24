"""Tests for blog_writer_agent helpers and blog_writing_process_v2 module-level
functions.

These cover small pure helpers and constructor / validation paths — the heavy
``run_pipeline`` orchestrator stays out of scope.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# blog_writer_agent.agent helpers
# ---------------------------------------------------------------------------


def test_extract_draft_after_marker_marker_present() -> None:
    from agents.blogging.blog_writer_agent.agent import _extract_draft_after_marker

    raw = '{"draft": 0}\n---DRAFT---\n# Hello\n\nBody\n'
    assert _extract_draft_after_marker(raw).startswith("# Hello")


def test_extract_draft_after_marker_marker_inline() -> None:
    from agents.blogging.blog_writer_agent.agent import _extract_draft_after_marker

    raw = "---DRAFT---# Hi"
    assert _extract_draft_after_marker(raw).startswith("# Hi")


def test_extract_draft_after_marker_falls_back_to_json() -> None:
    from agents.blogging.blog_writer_agent.agent import _extract_draft_after_marker

    raw = '{"draft": "# Title\\n\\nBody"}'
    out = _extract_draft_after_marker(raw)
    assert "Title" in out


def test_extract_draft_after_marker_falls_back_to_fenced_json() -> None:
    from agents.blogging.blog_writer_agent.agent import _extract_draft_after_marker

    raw = '```json\n{"draft": "# Fenced\\n\\nBody"}\n```'
    out = _extract_draft_after_marker(raw)
    assert "Fenced" in out


def test_extract_draft_after_marker_empty_inputs() -> None:
    from agents.blogging.blog_writer_agent.agent import _extract_draft_after_marker

    assert _extract_draft_after_marker("") == ""
    assert _extract_draft_after_marker(None) == ""  # type: ignore[arg-type]
    assert _extract_draft_after_marker("not json and no marker") == ""


def test_write_draft_to_path_creates_parents(tmp_path: Path) -> None:
    from agents.blogging.blog_writer_agent.agent import _write_draft_to_path

    target = tmp_path / "a" / "b" / "draft.md"
    _write_draft_to_path("# draft\n", target)
    assert target.read_text() == "# draft\n"


@pytest.mark.parametrize("draft", [None, 123])
def test_write_draft_to_path_rejects_non_string_draft(tmp_path: Path, draft: object) -> None:
    from agents.blogging.blog_writer_agent.agent import _write_draft_to_path

    target = tmp_path / "draft.md"
    with pytest.raises(TypeError, match="draft must be a string"):
        _write_draft_to_path(draft, target)  # type: ignore[arg-type]
    assert not target.exists()


@pytest.mark.parametrize("path", [None, 123])
def test_write_draft_to_path_rejects_invalid_path(tmp_path: Path, path: object) -> None:
    from agents.blogging.blog_writer_agent.agent import _write_draft_to_path

    with pytest.raises(TypeError, match="path must be a str or Path"):
        _write_draft_to_path("# draft\n", path)  # type: ignore[arg-type]


def test_writer_agent_requires_guidelines() -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    # Initialised without guidelines is allowed; only revise/draft enforces.
    agent = BlogWriterAgent(llm_client=DummyLLMClient())
    with pytest.raises(ValueError, match="brand"):
        agent._assert_guidelines_present()


def test_writer_agent_assertion_on_none_llm() -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    with pytest.raises(AssertionError):
        BlogWriterAgent(llm_client=None)


def test_writer_agent_style_prompt_merge() -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    a = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style A",
        brand_spec_content="Brand B",
    )
    assert "BRAND SPEC" in a._style_prompt
    assert "Brand B" in a._style_prompt
    assert "WRITING STYLE GUIDE" in a._style_prompt


def test_writer_agent_deterministic_self_check() -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    a = BlogWriterAgent(llm_client=DummyLLMClient())
    # Em-dash, banned phrase, vague citation, few 'you', staccato prose
    bad = (
        "In today's fast-paced world—as we navigate change. "
        "Studies show this works.\n\n"
        "Word. Word. Word.\n"
    )
    out = a._deterministic_self_check(bad)
    joined = "\n".join(out)
    assert "Em/en dash" in joined
    assert "Banned phrase" in joined
    assert "Vague citation" in joined or "Reader address" in joined


def test_writer_agent_deterministic_self_check_clean_draft() -> None:
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    a = BlogWriterAgent(llm_client=DummyLLMClient())
    clean = (
        "# Header\n\n"
        "You are reading something. You will learn. You will see. "
        "We covered tracing, costs, and evaluation in real workloads.\n\n"
        "These are pragmatic and useful for shipping high-quality software in your team's stack.\n"
    )
    out = a._deterministic_self_check(clean)
    # May still have some, but should be small
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# blog_writing_process_v2 module-level helpers
# ---------------------------------------------------------------------------


def test_v2_extract_story_placeholders() -> None:
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_story_placeholders,
    )

    draft = """# Post

[Author: add a moment you debugged a production outage]
Some paragraph.
[Author: insert a story about your favourite tool]
"""
    out = _extract_story_placeholders(draft)
    assert len(out) == 2
    topics = [t for _, t in out]
    assert any("outage" in t for t in topics)
    assert any("favourite tool" in t for t in topics)


def test_v2_extract_plan_keywords() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="Designing scalable systems and APIs",
        sections=[
            SimpleNamespace(title="Introduction to scale", order=0),
            SimpleNamespace(title="Practical tradeoffs", order=1),
        ],
    )
    kws = _extract_plan_keywords(plan)
    # Stopwords like 'and', 'to' should be dropped regardless of length; longer
    # content words kept.
    assert "scalable" in kws
    assert "systems" in kws
    assert "and" not in kws
    assert "to" not in kws


def test_v2_extract_plan_keywords_keeps_short_acronyms() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="Improving UX and AI-driven API design",
        sections=[
            SimpleNamespace(title="SQL query tuning", order=0),
        ],
    )
    kws = _extract_plan_keywords(plan)
    # Short but meaningful acronyms must survive the stopword filter.
    assert "ux" in kws
    assert "api" in kws
    assert "sql" in kws


def test_v2_extract_plan_keywords_drops_long_stopwords() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="A guide about your favourite tools",
        sections=[],
    )
    kws = _extract_plan_keywords(plan)
    # "about" and "your" are >= 4 chars but are stopwords, so should be dropped
    # despite the old length-only heuristic keeping them.
    assert "about" not in kws
    assert "your" not in kws
    assert "guide" in kws
    assert "favourite" in kws


def test_v2_extract_plan_keywords_drops_short_pronouns() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="We asked us if my team could help me",
        sections=[],
    )
    kws = _extract_plan_keywords(plan)
    # First/third-person pronouns are short (<4 chars) but survive the pure
    # length floor, so they must be dropped via the stopword list explicitly
    # to avoid spurious story-bank matches on words like "we" or "my".
    assert "we" not in kws
    assert "us" not in kws
    assert "my" not in kws
    assert "me" not in kws
    assert "if" not in kws
    assert "team" in kws
    assert "help" in kws


def test_v2_extract_plan_keywords_drops_punctuation_only_tokens() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="Migration guide -- ## legacy systems",
        sections=[],
    )
    kws = _extract_plan_keywords(plan)
    # Standalone punctuation tokens (e.g. "--", "##") have no alphanumeric
    # content and must not be treated as keywords, since they'd otherwise
    # cause spurious story-bank matches on formatting artifacts alone.
    assert "--" not in kws
    assert "##" not in kws
    assert "migration" in kws
    assert "legacy" in kws


def test_v2_extract_plan_keywords_ambiguous_short_words_filtered_regardless_of_case() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="IT in the US",
        sections=[
            SimpleNamespace(title="Notes from the office", order=0),
        ],
    )
    kws = _extract_plan_keywords(plan)
    # "it"/"us" are genuinely ambiguous between a domain acronym and the
    # pronouns "it"/"us"; casing can't reliably disambiguate them (an
    # all-caps heading doesn't mean every word is an acronym -- see
    # test_v2_extract_plan_keywords_all_caps_heading_does_not_admit_stopwords),
    # so they're filtered like any other stopword regardless of case, and
    # they aren't in the ``_PLAN_KEYWORD_SHORT_TERMS`` allowlist.
    assert "it" not in kws
    assert "us" not in kws
    assert "in" not in kws
    assert "the" not in kws
    assert "notes" in kws
    assert "office" in kws


def test_v2_extract_plan_keywords_lowercase_pronouns_still_filtered() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="We asked if it could work for us",
        sections=[],
    )
    kws = _extract_plan_keywords(plan)
    assert "it" not in kws
    assert "us" not in kws
    assert "we" not in kws


def test_v2_extract_plan_keywords_short_terms_admitted_regardless_of_casing() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="A guide to the Api and sql basics",
        sections=[SimpleNamespace(title="UX vs ai tradeoffs", order=0)],
    )
    kws = _extract_plan_keywords(plan)
    # Short domain terms are admitted via a fixed allowlist rather than by
    # inferring "acronym" from casing -- the planning LLM doesn't reliably
    # capitalize acronyms, so "Api" (title case) and "sql" (lowercase) must
    # be recognized exactly like "UX" (all caps).
    assert "api" in kws
    assert "sql" in kws
    assert "ux" in kws
    assert "ai" in kws


def test_v2_extract_plan_keywords_all_caps_heading_does_not_admit_stopwords() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="HOW TO USE AI",
        sections=[],
    )
    kws = _extract_plan_keywords(plan)
    # An all-caps heading must not be treated as "every word is an acronym";
    # ordinary stopwords ("how", "to", "use") stay filtered even in caps.
    # Only "ai" is admitted, via the allowlist.
    assert "how" not in kws
    assert "to" not in kws
    assert "use" not in kws
    assert "ai" in kws


def test_v2_extract_plan_keywords_includes_hardware_and_networking_terms() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="GPU vs CPU for inference",
        sections=[SimpleNamespace(title="DNS and SSH basics", order=0)],
    )
    kws = _extract_plan_keywords(plan)
    assert "gpu" in kws
    assert "cpu" in kws
    assert "dns" in kws
    assert "ssh" in kws


def test_v2_extract_plan_keywords_strips_smart_quotes_and_markdown_emphasis() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic="A guide “about” **great** tools, explained—",
        sections=[SimpleNamespace(title="**AI** for teams", order=0)],
    )
    kws = _extract_plan_keywords(plan)
    # Smart quotes, markdown emphasis markers, and a trailing em-dash must be
    # trimmed so the underlying word is compared cleanly: "about" (wrapped in
    # curly quotes) is still recognized as a stopword, "great"/"explained"
    # (wrapped in markdown emphasis / trailed by a dash) are still recognized
    # as ordinary keywords, and "**AI**" still resolves to the "ai" allowlist
    # entry rather than surviving as a malformed token.
    assert "about" not in kws
    assert "“about”" not in kws
    assert "great" in kws
    assert "explained" in kws
    assert "explained—" not in kws
    assert "ai" in kws
    assert "**ai**" not in kws


def test_v2_extract_plan_keywords_filters_sentence_connectives() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(
        overarching_topic=(
            "Teams should adopt this pattern because it reduces risk, "
            "not without tradeoffs, until it proves itself against "
            "alternatives"
        ),
        sections=[],
    )
    kws = _extract_plan_keywords(plan)
    # A full-sentence "stance" topic is dense with subordinating
    # conjunctions/prepositions; these must be filtered like any other
    # stopword even though they're common in argumentative prose.
    assert "because" not in kws
    assert "without" not in kws
    assert "until" not in kws
    assert "against" not in kws
    assert "adopt" in kws
    assert "pattern" in kws
    assert "reduces" in kws
    assert "alternatives" in kws


def test_v2_extract_plan_keywords_drops_ordinary_short_words_below_length_floor() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    ai_plan = SimpleNamespace(overarching_topic="New AI tools", sections=[])
    sql_plan = SimpleNamespace(overarching_topic="New SQL workflows", sections=[])
    ai_kws = _extract_plan_keywords(ai_plan)
    sql_kws = _extract_plan_keywords(sql_plan)
    # "New" is an ordinary word, not an acronym, and short enough (3 chars)
    # that admitting it regardless of stopword status would let unrelated
    # plans match in the story bank purely on "new". Acronyms ("AI", "SQL")
    # still survive since they're capitalized in the original text.
    assert "new" not in ai_kws
    assert "new" not in sql_kws
    assert "ai" in ai_kws
    assert "sql" in sql_kws
    assert not (set(ai_kws) & set(sql_kws))


def test_v2_extract_plan_keywords_handles_empty() -> None:
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = type("P", (), {"overarching_topic": "", "sections": []})()
    assert _extract_plan_keywords(plan) == []


def test_v2_is_external_cancellation_temporal() -> None:
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _is_external_cancellation,
    )
    from temporalio.exceptions import CancelledError

    e = CancelledError("nope")
    assert _is_external_cancellation(e) is True
    assert _is_external_cancellation(RuntimeError("not cancellation")) is False


def test_v2_planning_llm_client_passthrough(monkeypatch) -> None:
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        build_plan_critic_agent,
        plan_critic_llm_client,
        planning_llm_client,
    )

    from llm_service import DummyLLMClient

    base = DummyLLMClient()
    # No override env vars → passthrough
    monkeypatch.delenv("BLOG_PLANNING_MODEL", raising=False)
    monkeypatch.delenv("BLOG_PLAN_CRITIC_MODEL", raising=False)
    monkeypatch.delenv("BLOG_PLAN_CRITIC_ENABLED", raising=False)

    assert planning_llm_client(base) is base
    assert plan_critic_llm_client(base) is base
    assert build_plan_critic_agent(base) is None


def test_v2_planning_llm_client_override_keeps_failover(monkeypatch) -> None:
    """With BLOG_PLANNING_MODEL / BLOG_PLAN_CRITIC_MODEL set, the helpers return a
    failover-preserving variant that pins the model (rather than collapsing to a
    single non-failover client)."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        plan_critic_llm_client,
        planning_llm_client,
    )

    from llm_service.factory import FailoverLLMClient

    base = FailoverLLMClient(lambda: [], lambda e, r, mo=None: None, lambda e, x: None)

    monkeypatch.setenv("BLOG_PLANNING_MODEL", "planning:70b")
    planning = planning_llm_client(base)
    assert isinstance(planning, FailoverLLMClient)
    assert planning is not base
    assert planning._model_override == "planning:70b"

    monkeypatch.setenv("BLOG_PLAN_CRITIC_MODEL", "critic:34b")
    critic = plan_critic_llm_client(base)
    assert critic._model_override == "critic:34b"


def test_v2_build_plan_critic_agent_when_enabled(monkeypatch) -> None:
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        build_plan_critic_agent,
    )

    from llm_service import DummyLLMClient

    monkeypatch.setenv("BLOG_PLAN_CRITIC_ENABLED", "true")
    base = DummyLLMClient()
    critic = build_plan_critic_agent(base)
    assert critic is not None


# ---------------------------------------------------------------------------
# ghost_writer_agent — pure helpers
# ---------------------------------------------------------------------------


def test_ghost_writer_no_experience_phrase() -> None:
    from agents.blogging.ghost_writer_agent.agent import _is_no_experience

    assert _is_no_experience("skip") is True
    assert _is_no_experience("SKIP.") is True
    assert _is_no_experience("none") is True
    assert _is_no_experience("I don't have any story") is True
    assert _is_no_experience("I haven't tried that") is True
    assert _is_no_experience("Yes I have a great one") is False
    assert _is_no_experience("nothing comes to mind here") is True


def test_ghost_writer_agent_construction() -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent is not None


def test_ghost_writer_extract_gaps_from_plan_no_opportunities() -> None:
    """find_story_gaps falls back to LLM when plan has no story_opportunity fields."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import DummyLLMClient

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="A", coverage_description="cov", order=0),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # When no story_opportunity on sections, _extract_gaps_from_plan returns []
    out = agent._extract_gaps_from_plan(plan)
    assert out == []


def test_ghost_writer_extract_gaps_from_plan_with_opportunities(monkeypatch) -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import DummyLLMClient

    from ._content_plan_test_utils import make_content_plan

    sec_a = ContentPlanSection(
        title="A", coverage_description="cov", order=0, story_opportunity="A debug story"
    )
    sec_b = ContentPlanSection(
        title="B",
        coverage_description="cov2",
        order=1,
        story_opportunity="A migration story",
    )
    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[sec_a, sec_b],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # Patch the seed generator to avoid LLM call
    monkeypatch.setattr(agent, "_generate_friendly_seeds", lambda opps: [f"seed-{o}" for o in opps])
    out = agent._extract_gaps_from_plan(plan)
    assert len(out) == 2
    assert out[0].section_title == "A"
    assert "seed-A debug story" == out[0].seed_question


def test_ghost_writer_generate_friendly_seeds_fallback(monkeypatch) -> None:
    """When the LLM call raises, _generate_friendly_seeds falls back to generic seeds."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())

    # Patch the Agent class globally inside ghost_writer_agent.agent
    import agents.blogging.ghost_writer_agent.agent as gw_agent

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("nope")

    monkeypatch.setattr(gw_agent, "Agent", _BoomAgent)
    out = agent._generate_friendly_seeds(["topic A.", "topic B."])
    assert len(out) == 2
    assert all("topic" in s.lower() for s in out)


def test_ghost_writer_generate_friendly_seeds_empty_input() -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._generate_friendly_seeds([]) == []
