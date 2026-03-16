"""
Example: run research agent, then blog review agent, then blog draft agent
with an iterative draft-editor loop.

The draft agent writes a draft, the copy editor provides feedback, and the
draft agent revises based on feedback. This loop runs a configurable number
of times (default: 3).
"""

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

import logging
from pathlib import Path

from shared.style_loader import load_style_file

logger = logging.getLogger(__name__)

from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.models import ResearchBriefInput
from llm_service import get_client
from blog_review_agent import BlogReviewAgent, BlogReviewInput
from blog_draft_agent import BlogDraftAgent, DraftInput, ReviseDraftInput
from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "brandon_kindred_brand_and_writing_style_guide.md"
BRAND_SPEC_PATH = _blogging_docs / "brand_spec.yaml"

# Number of draft-editor loop iterations (1 = draft only, no revisions; 100 = draft + 99 revision cycles)
DRAFT_EDITOR_ITERATIONS = 100

llm_client = get_client("blog")

# 1. Research
cache = AgentCache(cache_dir=".agent_cache")
research_agent = ResearchAgent(llm_client=llm_client, cache=cache)
brief = ResearchBriefInput(
    brief="LLM observability best practices for large enterprises",
    audience="CTOs and platform teams",
    tone_or_purpose="technical deep-dive",
    max_results=20,
)
research_result = research_agent.run(brief)
logger.info("RESEARCH RESULT DOCUMENT: \n ---------------------------------------- \n %s", research_result)

# 2. Blog review (titles + outline from brief + sources)
review_agent = BlogReviewAgent(llm_client=llm_client)
review_input = BlogReviewInput(
    brief=brief.brief,
    audience=brief.audience,
    tone_or_purpose=brief.tone_or_purpose,
    references=research_result.references,
)
review_result = review_agent.run(review_input)
logger.info("OUTLINE DOCUMENT: \n ---------------------------------------- \n %s", review_result)

# 3. Blog draft (research document + outline -> full draft)
research_document = research_result.compiled_document or ""
if not research_document and research_result.references:
    # Fallback: build minimal research doc from references
    parts = ["## Sources\n"]
    for ref in research_result.references:
        parts.append(f"- **{ref.title}** ({ref.url}): {ref.summary}")
        if ref.key_points:
            parts.append("  Key points: " + "; ".join(ref.key_points[:3]))
    research_document = "\n".join(parts)

writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
brand_spec_content = load_style_file(BRAND_SPEC_PATH, "brand spec")

draft_agent = BlogDraftAgent(
    llm_client=llm_client,
    writing_style_guide_content=writing_style_content,
    brand_spec_content=brand_spec_content,
)
copy_editor_agent = BlogCopyEditorAgent(
    llm_client=llm_client,
    writing_style_guide_content=writing_style_content,
    brand_spec_content=brand_spec_content,
)

# Draft-editor loop: draft -> editor feedback -> revise (repeat)
draft_result = None
for iteration in range(1, DRAFT_EDITOR_ITERATIONS + 1):
    if iteration == 1:
        # Initial draft
        draft_input = DraftInput(
            research_document=research_document,
            outline=review_result.outline,
            audience=brief.audience,
            tone_or_purpose=brief.tone_or_purpose,
        )
        draft_result = draft_agent.run(draft_input)
        logger.info("Draft iteration %s: initial draft, length=%s", iteration, len(draft_result.draft))
    else:
        # Copy editor reviews, then draft agent revises
        copy_editor_input = CopyEditorInput(
            draft=draft_result.draft,
            audience=brief.audience,
            tone_or_purpose=brief.tone_or_purpose,
        )
        # Pass feedback_output_path to persist editor feedback to a file when running with work_dir.
        copy_editor_result = copy_editor_agent.run(copy_editor_input)
        logger.info(
            "Copy editor iteration %s: %s feedback items",
            iteration,
            len(copy_editor_result.feedback_items),
        )

        revise_input = ReviseDraftInput(
            draft=draft_result.draft,
            feedback_items=copy_editor_result.feedback_items,
            feedback_summary=copy_editor_result.summary,
            research_document=research_document,
            outline=review_result.outline,
            audience=brief.audience,
            tone_or_purpose=brief.tone_or_purpose,
        )
        draft_result = draft_agent.revise(revise_input)
        logger.info("Draft iteration %s: revised, length=%s", iteration, len(draft_result.draft))

logger.info("DRAFT DOCUMENT: \n ---------------------------------------- \n %s", draft_result.draft[:2000] + ("..." if len(draft_result.draft) > 2000 else ""))
logger.info("Total LLM requests across blog agents: %s", llm_client.request_count)

# 4. Output
print("\n--- Top 10 title choices (with probability of success) ---")
for i, tc in enumerate(review_result.title_choices, 1):
    print(f"{i}. {tc.title}  [{tc.probability_of_success:.0%}]")
print("\n--- Blog outline ---\n")
print(review_result.outline)
print("\n--- Blog draft ---\n")
print(draft_result.draft)
