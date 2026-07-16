"""Tests for the story-bank save path in blog_writing_process_v2.

Focus: ``_save_narratives_to_story_bank`` persists each elicited narrative under the
exact story gap it was collected for. This locks in the fix for the fragile
substring-matching that a section title being a substring of another section's
narrative could otherwise defeat.
"""

from __future__ import annotations


def _gap(title: str, context: str = "ctx"):
    from ghost_writer_agent.models import StoryGap

    return StoryGap(section_title=title, section_context=context, seed_question="q?")


def test_save_narratives_pairs_each_narrative_with_its_own_gap(monkeypatch) -> None:
    """Overlapping section titles ('Intro' ⊂ 'Introduction') must not cross-match."""
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import story_bank

    calls: list[dict] = []
    monkeypatch.setattr(story_bank, "save_story", lambda **kw: calls.append(kw))

    # "Intro" is a substring of the narrative saved for "Introduction"; the old
    # substring search could have mis-associated these.
    pairs = [
        (_gap("Intro", "c-intro"), "This is the Introduction narrative, quite long."),
        (_gap("Introduction", "c-introduction"), "A short intro tale."),
    ]

    saved = v2._save_narratives_to_story_bank(
        pairs,
        topic_keywords=["python", "web"],
        job_id="job-1",
        llm_client=None,
    )

    assert saved == 2
    assert len(calls) == 2
    by_title = {c["section_title"]: c for c in calls}
    assert by_title["Intro"]["narrative"] == "This is the Introduction narrative, quite long."
    assert by_title["Intro"]["section_context"] == "c-intro"
    assert by_title["Introduction"]["narrative"] == "A short intro tale."
    assert by_title["Introduction"]["section_context"] == "c-introduction"
    # Shared metadata is forwarded verbatim to every save.
    for c in calls:
        assert c["keywords"] == ["python", "web"]
        assert c["source_job_id"] == "job-1"


def test_save_narratives_empty_pairs_is_noop(monkeypatch) -> None:
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import story_bank

    calls: list[dict] = []
    monkeypatch.setattr(story_bank, "save_story", lambda **kw: calls.append(kw))

    saved = v2._save_narratives_to_story_bank([], topic_keywords=[], job_id=None, llm_client=None)

    assert saved == 0
    assert calls == []
