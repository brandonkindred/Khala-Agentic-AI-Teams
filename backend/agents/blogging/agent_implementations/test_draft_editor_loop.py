"""
Quick test of the draft-editor loop using DummyLLMClient (no Ollama or API keys needed).
"""

try:
    from . import _path_setup  # noqa: F401
except ImportError:
    import _path_setup  # noqa: F401  # when run as script

import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from llm_service import DummyLLMClient
from shared.style_loader import load_style_file
from blog_review_agent import BlogReviewAgent, BlogReviewInput
from blog_research_agent.models import ResearchReference
from blog_draft_agent import BlogDraftAgent, DraftInput, ReviseDraftInput
from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "brandon_kindred_brand_and_writing_style_guide.md"
BRAND_SPEC_PATH = _blogging_docs / "brand_spec.yaml"
DRAFT_EDITOR_ITERATIONS = 100

# Fixed context (no research pipeline)
CONTEXT_BRIEF = "LLM observability best practices for large enterprises."
PLACEHOLDER_REF = ResearchReference(
    title="LLM Observability Guide",
    url="https://example.com/observability",
    summary="Best practices for monitoring LLMs in production.",
    key_points=["Tracing", "Cost attribution", "Prompt versioning"],
)
OUTLINE = """# Introduction
Hook on the importance of observability. Set stakes for CTOs.

# The Problem
Traditional monitoring falls short for LLMs. Non-deterministic outputs.

# What to Look For
Tracing, cost attribution, evaluation metrics.

# Wrap up
Recap and one practical next step."""

RESEARCH_DOC = "## Sources\n- LLM Observability Guide: Best practices for monitoring LLMs in production."


def main() -> None:
    llm = DummyLLMClient()
    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PATH, "brand spec")

    # Get outline from review agent
    review_agent = BlogReviewAgent(llm_client=llm)
    review_input = BlogReviewInput(
        brief=CONTEXT_BRIEF,
        audience="CTOs and platform teams",
        tone_or_purpose="technical deep-dive",
        references=[PLACEHOLDER_REF],
    )
    review_result = review_agent.run(review_input)
    outline = review_result.outline

    draft_agent = BlogDraftAgent(
        llm_client=llm,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )
    copy_editor_agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

    draft_result = None
    for iteration in range(1, DRAFT_EDITOR_ITERATIONS + 1):
        if iteration == 1:
            draft_input = DraftInput(
                research_document=RESEARCH_DOC,
                outline=outline,
                audience="CTOs and platform teams",
                tone_or_purpose="technical deep-dive",
            )
            draft_result = draft_agent.run(draft_input)
            print(f"\n--- Iteration {iteration}: Initial draft ({len(draft_result.draft)} chars) ---")
        else:
            copy_editor_input = CopyEditorInput(
                draft=draft_result.draft,
                audience="CTOs and platform teams",
                tone_or_purpose="technical deep-dive",
            )
            copy_editor_result = copy_editor_agent.run(copy_editor_input)
            print(f"\n--- Iteration {iteration}: Copy editor found {len(copy_editor_result.feedback_items)} feedback items ---")

            revise_input = ReviseDraftInput(
                draft=draft_result.draft,
                feedback_items=copy_editor_result.feedback_items,
                feedback_summary=copy_editor_result.summary,
                research_document=RESEARCH_DOC,
                outline=outline,
                audience="CTOs and platform teams",
                tone_or_purpose="technical deep-dive",
            )
            draft_result = draft_agent.revise(revise_input)
            print(f"--- Iteration {iteration}: Revised draft ({len(draft_result.draft)} chars) ---")

    print("\n" + "=" * 60)
    print("FINAL DRAFT (first 800 chars):")
    print("=" * 60)
    print(draft_result.draft[:800])
    if len(draft_result.draft) > 800:
        print("\n... [truncated] ...")
    print("\n✓ Draft-editor loop completed successfully!")


if __name__ == "__main__":
    main()
