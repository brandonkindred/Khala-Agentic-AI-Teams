"""Shape tests for the merged doc score+summarize prompt in blog_research_agent
(``prompts.DOC_SCORE_AND_SUMMARIZE_PROMPT``).

This prompt is not wired into ``ResearchAgent`` yet -- per issue #7870 (story 1
of epic #7863), it deliberately has no call site: ``_score_one_document`` and
``_summarize_one_document`` still run their original, separate prompts. These
tests only verify the design itself: that the merged prompt carries both
source prompts' full instruction content verbatim under separate headings,
in order, and that its response-shape section names all six combined keys.
Wiring it into the agent's scoring/summarization fan-outs is a follow-up
story.
"""

from agents.blogging.blog_research_agent.prompts import (
    DOC_RELEVANCE_SCORING_PROMPT,
    DOC_SCORE_AND_SUMMARIZE_PROMPT,
    DOC_SUMMARIZATION_PROMPT,
)


def test_merged_prompt_contains_both_source_bodies_verbatim() -> None:
    assert DOC_RELEVANCE_SCORING_PROMPT in DOC_SCORE_AND_SUMMARIZE_PROMPT
    assert DOC_SUMMARIZATION_PROMPT in DOC_SCORE_AND_SUMMARIZE_PROMPT


def test_merged_prompt_has_scoring_and_summarization_sections_in_order() -> None:
    # Each heading must precede the body it introduces, and scoring must come
    # before summarization -- not just "both headings appear somewhere".
    p = DOC_SCORE_AND_SUMMARIZE_PROMPT
    assert (
        p.index("## Scoring")
        < p.index(DOC_RELEVANCE_SCORING_PROMPT)
        < p.index("## Summarization")
        < p.index(DOC_SUMMARIZATION_PROMPT)
        < p.index("## Response shape")
    )


def test_merged_prompt_names_all_six_response_keys() -> None:
    # Assert against the "## Response shape" section specifically, not the whole
    # prompt: every key below also appears inside the embedded source prompt
    # bodies (describing their own outputs), so a whole-prompt match would still
    # pass even if the response-shape section itself dropped a key. Match the
    # exact bullet line, not a bare substring, so a rename to a superset key
    # (e.g. "type" -> "document_type") is caught rather than silently matched.
    response_shape = DOC_SCORE_AND_SUMMARIZE_PROMPT.split("## Response shape", 1)[1]
    for key, spec in (
        ("relevance_score", "float between 0 and 1"),
        ("authority_score", "float between 0 and 1"),
        ("accuracy_score", "float between 0 and 1"),
        ("type", "string"),
        ("summary", "string"),
        ("key_points", "list of strings"),
    ):
        assert f"- {key}: {spec}" in response_shape


def test_merged_prompt_requests_single_json_object_no_markdown() -> None:
    lowered = DOC_SCORE_AND_SUMMARIZE_PROMPT.lower()
    assert "single json object" in lowered
    assert "no markdown" in lowered
    assert "no code fence" in lowered
