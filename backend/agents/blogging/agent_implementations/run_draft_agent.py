"""
Example: run the blog draft agent with a research document and outline.

Loads the Brandon Kindred style guide from docs/ and generates a draft
that complies with it. Pass your own research_document and outline, or
use placeholders for testing.
"""

from . import _path_setup  # noqa: F401  # Add blogging to path when run from project root

import logging
from pathlib import Path

from blog_research_agent.llm import OllamaLLMClient  # or DummyLLMClient for quick test
from blog_draft_agent import BlogDraftAgent, DraftInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Path to the style guide (relative to project root when run from repo)
STYLE_GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "brandon_kindred_brand_and_writing_style_guide.md"


def main() -> None:
    llm_client = OllamaLLMClient()
    # Or: from blog_research_agent.llm import DummyLLMClient; llm_client = DummyLLMClient()

    agent = BlogDraftAgent(llm_client=llm_client, default_style_guide_path=STYLE_GUIDE_PATH)

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
        style_guide=STYLE_GUIDE_PATH.read_text().strip() if STYLE_GUIDE_PATH.exists() else None,
    )

    result = agent.run(draft_input)
    print("\n--- Draft (first 2000 chars) ---\n")
    print(result.draft[:2000])
    if len(result.draft) > 2000:
        print("\n... [truncated] ...")


if __name__ == "__main__":
    main()
