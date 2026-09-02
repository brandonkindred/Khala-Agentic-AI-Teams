"""Shape tests for the merged doc score+summarize prompt in blog_research_agent."""

from agents.blogging.blog_research_agent.prompts import (
    DOC_RELEVANCE_SCORING_PROMPT,
    DOC_SCORE_AND_SUMMARIZE_PROMPT,
    DOC_SUMMARIZATION_PROMPT,
)


def test_merged_prompt_contains_both_source_bodies_verbatim() -> None:
    assert DOC_RELEVANCE_SCORING_PROMPT in DOC_SCORE_AND_SUMMARIZE_PROMPT
    assert DOC_SUMMARIZATION_PROMPT in DOC_SCORE_AND_SUMMARIZE_PROMPT


def test_merged_prompt_names_all_six_response_keys() -> None:
    for key in (
        "relevance_score",
        "authority_score",
        "accuracy_score",
        "type",
        "summary",
        "key_points",
    ):
        assert key in DOC_SCORE_AND_SUMMARIZE_PROMPT


def test_merged_prompt_requests_single_json_object_no_markdown() -> None:
    lowered = DOC_SCORE_AND_SUMMARIZE_PROMPT.lower()
    assert "json object" in lowered
    assert "no markdown" in lowered
    assert "no code fence" in lowered
