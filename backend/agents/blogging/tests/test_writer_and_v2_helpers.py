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
    from .conftest import make_writer_agent

    # Empty guidelines preserve constructor-default behavior (factory defaults are non-empty).
    agent = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    with pytest.raises(ValueError, match="brand"):
        agent._assert_guidelines_present()


def test_writer_agent_raises_on_none_llm() -> None:
    """A ``None`` llm_client raises ValueError (not assert, which -O can strip)."""
    from agents.blogging.blog_writer_agent.agent import BlogWriterAgent

    # Direct construction intentional: make_writer_agent() replaces None with DummyLLMClient.
    with pytest.raises(ValueError, match="llm_client"):
        BlogWriterAgent(llm_client=None)


def test_writer_agent_style_prompt_merge() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(
        writing_style_guide_content="Style A",
        brand_spec_content="Brand B",
    )
    assert "BRAND SPEC" in a._style_prompt
    assert "Brand B" in a._style_prompt
    assert "WRITING STYLE GUIDE" in a._style_prompt


def test_writer_agent_deterministic_self_check() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
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


def test_writer_agent_deterministic_self_check_vague_citation_with_https_link() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    draft = "Studies show this works, as covered in [this source](https://example.com/report).\n"
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Vague citation" not in joined


def test_writer_agent_deterministic_self_check_clean_draft() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    clean = (
        "# Header\n\n"
        "You are reading something. You will learn. You will see. "
        "We covered tracing, costs, and evaluation in real workloads.\n\n"
        "These are pragmatic and useful for shipping high-quality software in your team's stack.\n"
    )
    out = a._deterministic_self_check(clean)
    # Three short sentences in a row ("You are reading something." / "You will
    # learn." / "You will see.") form one staccato streak; nothing else in this
    # draft trips the other checks (reader-address count is 4, no banned
    # phrases/dashes/vague citations).
    assert out == ["Staccato prose in paragraph 2: 3+ consecutive short sentences"]


def test_writer_agent_deterministic_self_check_rejects_non_string() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    with pytest.raises(TypeError, match="draft must be a string"):
        a._deterministic_self_check(None)  # type: ignore[arg-type]


def test_writer_agent_deterministic_self_check_crlf_paragraphs() -> None:
    """Windows-style blank lines still split into paragraphs for dash detection."""
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    draft = "First paragraph with an em—dash.\r\n\r\nSecond paragraph without dashes.\r\n"
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Em/en dash found in paragraph 1" in joined


def test_writer_agent_deterministic_self_check_yourselves_counts() -> None:
    """Plural reflexive 'yourselves' counts toward the reader-address minimum."""
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    draft = (
        "Ask yourselves what matters. Challenge yourselves to ship. "
        "Remind yourselves why the work counts for real teams every day.\n"
    )
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Reader address" not in joined


def test_writer_agent_deterministic_self_check_abbrev_not_staccato() -> None:
    """Mid-sentence abbreviation periods must not create a false staccato streak."""
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    # Long sentences with mid-sentence abbrevs (lowercase continuation). Without
    # case-sensitive lookahead protection these would split on internal periods.
    draft = (
        "You should evaluate observability tools, e.g. tracers and profilers, "
        "before committing your team to a vendor. "
        "You can also prefer clearer wording, i.e. deeper probes over slogans. "
        "You will see U.S. teams succeed with careful measurement across releases.\n"
    )
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Staccato" not in joined


def test_writer_agent_deterministic_self_check_abbrev_lookahead_case_sensitive() -> None:
    """``re.IGNORECASE`` must not make the ``[A-Z]`` sentence-boundary test ignore case."""
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    # If ``[A-Z]`` were case-insensitive, ``e.g. tracing`` would not be protected and
    # the following short sentences would form a false staccato streak.
    draft = (
        "Please carefully review the available options, e.g. tracing platforms. Go now. Ship it.\n"
    )
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Staccato" not in joined


def test_writer_agent_deterministic_self_check_terminal_abbrev_preserves_boundary() -> None:
    """A sentence-ending abbreviation period must still count as a sentence boundary."""
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    # Three short sentences; the first ends with ``etc.`` — that period is real.
    draft = "Use standards, etc. Test carefully. Ship confidently.\n"
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Staccato" in joined


def test_writer_agent_deterministic_self_check_terminal_us_preserves_boundary() -> None:
    """Sentence-ending ``U.S.`` must keep its terminal period for staccato detection."""
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="", brand_spec_content="")
    draft = "We moved to the U.S. Act now. Go fast.\n"
    out = a._deterministic_self_check(draft)
    joined = "\n".join(out)
    assert "Staccato" in joined


def test_writer_agent_call_agent_rejects_empty_prompt() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="s", brand_spec_content="b")
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        a._call_agent(a._model, "   ")


def test_writer_agent_fallback_draft_rejects_empty_prompt() -> None:
    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="s", brand_spec_content="b")
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        a._fallback_draft_via_json("")


def test_writer_agent_format_feedback_item_rejects_non_positive_index() -> None:
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem

    from .conftest import make_writer_agent

    a = make_writer_agent(writing_style_guide_content="s", brand_spec_content="b")
    item = FeedbackItem(category="x", severity="minor", issue="i")
    with pytest.raises(ValueError, match="index must be a positive int"):
        a._format_feedback_item_line(item, 0)


def test_write_draft_to_path_rejects_parent_traversal(tmp_path: Path) -> None:
    from agents.blogging.blog_writer_agent.agent import _write_draft_to_path

    with pytest.raises(ValueError, match="must not contain '\\.\\.'"):
        _write_draft_to_path("draft", tmp_path / ".." / "escape.md")


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
    # still survive because they're in the _PLAN_KEYWORD_SHORT_TERMS
    # allowlist, regardless of casing.
    assert "new" not in ai_kws
    assert "new" not in sql_kws
    assert "ai" in ai_kws
    assert "sql" in sql_kws
    assert not (set(ai_kws) & set(sql_kws))


def test_v2_extract_plan_keywords_handles_empty() -> None:
    from types import SimpleNamespace

    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _extract_plan_keywords,
    )

    plan = SimpleNamespace(overarching_topic="", sections=[])
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
