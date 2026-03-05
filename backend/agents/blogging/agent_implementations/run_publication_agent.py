"""
Example: run the blog publication agent.

1. Submit a draft -> written to blog_posts/pending/
2. Approve or reject (can be seconds or days later)
3. On approve: creates folder with draft.md, medium.md, devto.md, substack.md
4. On reject: agent asks follow-up questions; when ready, run_revision_loop() to revise

This script demonstrates the full flow. In practice, approval/rejection may come
from a CLI prompt, API, or another system.
"""

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

import logging
from pathlib import Path

from blog_research_agent.llm import OllamaLLMClient
from blog_copy_editor_agent import BlogCopyEditorAgent
from blog_draft_agent import BlogDraftAgent
from blog_publication_agent import (
    BlogPublicationAgent,
    SubmitDraftInput,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STYLE_GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "brandon_kindred_brand_and_writing_style_guide.md"

EXAMPLE_DRAFT = """# Why LLM Observability Matters for Enterprise AI

I thought I understood scale. Then I joined a team running LLMs in production.

That was the moment I realized something. Observability for AI systems is different. Traditional monitoring tools were not built for non-deterministic outputs. You need visibility into prompts, responses, latency, token usage, and cost, all in one place.

## The Problem

When something goes wrong with an LLM in production, debugging can feel like finding a needle in a haystack. You need tracing, cost attribution, and prompt versioning.

## What to Look For

1. Real-time tracing
2. Cost attribution per request
3. Evaluation metrics

## Conclusion

Implementing LLM observability is non-negotiable for any serious enterprise AI initiative. If you try this, tell me what broke. I want to hear the war stories.
"""


def main() -> None:
    llm = OllamaLLMClient()
    style_guide = STYLE_GUIDE_PATH.read_text().strip() if STYLE_GUIDE_PATH.exists() else None

    publication_agent = BlogPublicationAgent(
        llm_client=llm,
        blog_posts_root=Path(__file__).resolve().parent.parent / "blog_posts",
        max_revision_loops=3,
    )

    draft_agent = BlogDraftAgent(llm_client=llm, default_style_guide_path=STYLE_GUIDE_PATH)
    copy_editor_agent = BlogCopyEditorAgent(llm_client=llm, default_style_guide_path=STYLE_GUIDE_PATH)

    # 1. Submit draft
    result = publication_agent.submit_draft(
        SubmitDraftInput(
            draft=EXAMPLE_DRAFT,
            audience="CTOs and platform teams",
            tone_or_purpose="technical deep-dive",
            tags=["llm", "observability", "enterprise"],
        )
    )
    print(f"\n--- Submitted ---\nsubmission_id = {result.submission_id}\n{result.message}")

    # At this point, the draft is in blog_posts/pending/. The human can approve
    # or reject at any time (seconds or days later).

    # 2a. APPROVE (uncomment to run):
    # approval = publication_agent.approve(result.submission_id)
    # print(f"\n--- Approved ---\n{approval.message}\nFolder: {approval.folder_path}")

    # 2b. REJECT (uncomment to run):
    # rejection = publication_agent.reject(
    #     result.submission_id,
    #     "The intro is too vague. I want a specific story about a real outage.",
    # )
    # if rejection.questions:
    #     print("Follow-up questions:", rejection.questions)
    # if rejection.ready_to_revise:
    #     revision = publication_agent.run_revision_loop(
    #         result.submission_id,
    #         draft_agent=draft_agent,
    #         copy_editor_agent=copy_editor_agent,
    #         audience="CTOs and platform teams",
    #         tone_or_purpose="technical deep-dive",
    #         style_guide=style_guide,
    #     )
    #     print(f"\n--- Revised ---\n{revision.message}")
    #     print(f"Draft updated ({revision.iterations_completed} iterations).")


if __name__ == "__main__":
    main()
