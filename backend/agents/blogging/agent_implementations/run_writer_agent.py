"""
Example: run the blog draft agent with a research document and outline.

Loads the author's style guide from docs/ (rendered against the configured
author profile) and generates a draft that complies with it. Pass your own
research_document and outline, or use placeholders for testing.
"""

import logging
from pathlib import Path

from blog_writer_agent import BlogWriterAgent, WriterInput
from shared.style_loader import load_style_file

from llm_service import get_strands_model

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"


def main() -> None:
    llm_client = get_strands_model("blog")

    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")
    agent = BlogWriterAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

    # Example research document and outline (e.g. from research + review agents)
    _research_document = """
Compiled Research: Most Relevant Sources
Topic: Building an AI Agent with Strands

1. https://example.com/strands-docs - Strands is a model-driven SDK. Summary: Reduces boilerplate...
2. https://example.com/agents-guide - Beginner-friendly. Key points: Setup, run, deploy.
"""
    _outline = """
# Introduction to AI Agents and Strands
Explain agentic AI and Strands as a beginner-friendly SDK.

# Setup and Installation
Step-by-step install and code snippets.

# Basic Agent Creation
Minimal code example for a simple agent.

# Wrap up
Recap and one practical next step.
"""

    draft_input = WriterInput(
        content_plan=None,  # type: ignore[arg-type]  # test harness — provide a real ContentPlan
        audience="Beginners to AI Agents",
        tone_or_purpose="Educational",
    )

    result = agent.run(draft_input)
    print("\n--- Draft (first 2000 chars) ---\n")
    print(result.draft[:2000])
    if len(result.draft) > 2000:
        print("\n... [truncated] ...")


if __name__ == "__main__":
    main()
