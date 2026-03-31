"""
Example: run the blog publication agent.

1. Submit a draft -> written to blog_posts/pending/
2. Approve or reject (can be seconds or days later)
3. On approve: creates folder with draft.md, medium.md, devto.md, substack.md
4. On reject: agent asks follow-up questions; when ready, run_revision_loop() to revise

This script demonstrates the full flow. In practice, approval/rejection may come
from a CLI prompt, API, or another system.
"""

import logging
from pathlib import Path

from blog_copy_editor_agent import BlogCopyEditorAgent
from blog_publication_agent import (
    BlogPublicationAgent,
    SubmitDraftInput,
)
from blog_writer_agent import BlogWriterAgent
from shared.style_loader import load_style_file

from llm_service import get_client

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"

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
    llm = get_client("blog")
    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")

    publication_agent = BlogPublicationAgent(
        llm_client=llm,
        blog_posts_root=Path(__file__).resolve().parent.parent / "blog_posts",
        max_revision_loops=3,
    )

    BlogWriterAgent(
        llm_client=llm,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )
    BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

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
    #     )
    #     print(f"\n--- Revised ---\n{revision.message}")
    #     print(f"Draft updated ({revision.iterations_completed} iterations).")


if __name__ == "__main__":
    main()
