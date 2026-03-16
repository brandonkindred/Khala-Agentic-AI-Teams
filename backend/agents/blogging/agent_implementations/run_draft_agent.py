"""
Example: run the blog draft agent with a research document and outline.

Loads the Brandon Kindred style guide from docs/ and generates a draft
that complies with it. Pass your own research_document and outline, or
use placeholders for testing.
"""

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

import logging
from pathlib import Path

from llm_service import get_client  # or DummyLLMClient for quick test
from shared.style_loader import load_style_file
from blog_draft_agent import BlogDraftAgent, DraftInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

_blogging_docs = Path(__file__).resolve().parent.parent / "docs"
STYLE_GUIDE_PATH = _blogging_docs / "writing_guidelines.md"
BRAND_SPEC_PROMPT_PATH = _blogging_docs / "brand_spec_prompt.md"


def main() -> None:
    llm_client = get_client("blog")
    # Or: from llm_service import DummyLLMClient; llm_client = DummyLLMClient()

    writing_style_content = load_style_file(STYLE_GUIDE_PATH, "writing style guide")
    brand_spec_content = load_style_file(BRAND_SPEC_PROMPT_PATH, "brand spec prompt")
    agent = BlogDraftAgent(
        llm_client=llm_client,
        writing_style_guide_content=writing_style_content,
        brand_spec_content=brand_spec_content,
    )

    # Example: use your research document and outline (e.g. from research + review agents)
    research_document = """
Compiled Research: Most Relevant Sources
Topic: Building an AI Agent with Strands

1. https://example.com/strands-docs - Strands is a model-driven SDK. Summary: Reduces boilerplate...
2. https://example.com/agents-guide - Beginner-friendly. Key points: Setup, run, deploy.
"""
    outline = """
# Introduction to AI Agents and Strands
Explain agentic AI and Strands as a beginner-friendly SDK.

# Setup and Installation
Step-by-step install and code snippets.

# Basic Agent Creation
Minimal code example for a simple agent.

# Wrap up
Recap and one practical next step.
"""

    draft_input = DraftInput(
        research_document=research_document.strip(),
        outline=outline.strip(),
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
