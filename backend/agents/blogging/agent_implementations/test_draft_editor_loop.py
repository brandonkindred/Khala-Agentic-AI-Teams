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

from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput  # noqa: E402
from blog_draft_agent import BlogDraftAgent, DraftInput, ReviseDraftInput  # noqa: E402
from blog_research_agent.models import ResearchReference  # noqa: E402
from shared.content_plan import (  # noqa: E402
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
)
from shared.style_loader import load_style_file  # noqa: E402

from llm_service import DummyLLMClient  # noqa: E402

# Keep in sync with DRAFT_EDITOR_ITERATIONS in blog_writing_process_v2.py
DRAFT_EDITOR_ITERATIONS = 500

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"

CONTEXT_BRIEF = "LLM observability best practices for large enterprises."
PLACEHOLDER_REF = ResearchReference(
    title="LLM Observability Guide",
    url="https://example.com/observability",
    summary="Best practices for monitoring LLMs in production.",
    key_points=["Tracing", "Cost attribution", "Prompt versioning"],
)
CONTENT_PLAN = ContentPlan(
    overarching_topic="LLM observability for enterprises",
    narrative_flow="Problem, practices, wrap-up.",
    sections=[
        ContentPlanSection(title="Introduction", coverage_description="Hook and stakes.", order=0),
        ContentPlanSection(title="The Problem", coverage_description="Why classic monitoring fails.", order=1),
        ContentPlanSection(title="What to Look For", coverage_description="Tracing, cost, evals.", order=2),
        ContentPlanSection(title="Wrap up", coverage_description="One next step.", order=3),
    ],
    title_candidates=[TitleCandidate(title="Observability essentials", probability_of_success=0.7)],
    requirements_analysis=RequirementsAnalysis(
        plan_acceptable=True,
        scope_feasible=True,
        research_gaps=[],
    ),
)

RESEARCH_DOC = "## Sources\n- LLM Observability Guide: Best practices for monitoring LLMs in production."


def main() -> None:
    llm = DummyLLMClient()
    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")

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
                content_plan=CONTENT_PLAN,
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
                content_plan=CONTENT_PLAN,
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
