"""
Example: run the blog copy editor agent on a draft.

Loads the author's style guide from docs/ (rendered against the configured
author profile) and provides feedback on how well the draft aligns with
the brand and writing style.
"""

import logging
from pathlib import Path

from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput
from shared.style_loader import load_style_file

from llm_service import get_client

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"

# Example draft to review (replace with your own or load from file)
EXAMPLE_DRAFT = """
# Why LLM Observability Matters for Enterprise AI

In today's fast-paced world—enterprises are increasingly adopting large language models to power everything from customer support to internal tooling. But here's the catch: without proper observability, you're flying blind.

## The Problem

When something goes wrong with an LLM in production, debugging can feel like finding a needle in a haystack. Traditional monitoring tools weren't designed for the non-deterministic nature of AI systems. You need visibility into prompts, responses, latency, token usage, and cost—all in one place.

## What to Look For

- Real-time tracing
- Cost attribution
- Prompt versioning
- Evaluation metrics

## Conclusion

Implementing LLM observability is non-negotiable for any serious enterprise AI initiative. Make sure you choose a solution that fits your stack.
"""


def main() -> None:
    llm_client = get_client("blog")

    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")
    agent = BlogCopyEditorAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

    copy_editor_input = CopyEditorInput(
        draft=EXAMPLE_DRAFT.strip(),
        audience="CTOs and platform teams",
        tone_or_purpose="technical deep-dive",
    )

    result = agent.run(copy_editor_input)

    print("\n--- Copy Editor Summary ---\n")
    print(result.summary)
    print("\n--- Feedback Items ---\n")
    for i, item in enumerate(result.feedback_items, 1):
        loc = f" [{item.location}]" if item.location else ""
        print(f"{i}. [{item.severity}] {item.category}{loc}")
        print(f"   Issue: {item.issue}")
        if item.suggestion:
            print(f"   Suggestion: {item.suggestion}")
        print()


if __name__ == "__main__":
    main()
