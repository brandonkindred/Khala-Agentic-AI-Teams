"""
Example: run the blog draft agent with a research document and outline.

Loads the author's style guide from docs/ (rendered against the configured
author profile) and generates a draft that complies with it. Pass your own
research_document and outline, or use placeholders for testing.
"""

import logging
from pathlib import Path

from agents.blogging.blog_writer_agent import BlogWriterAgent, WriterInput
from agents.blogging.shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
)
from agents.blogging.shared.style_loader import load_style_file

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

    # A minimal but valid ContentPlan so the example is runnable end-to-end
    # (WriterInput requires a real content_plan). Replace with a plan produced by
    # the planning agent for a non-placeholder run.
    content_plan = ContentPlan(
        overarching_topic="Building an AI Agent with Strands",
        narrative_flow="Introduce agents, set up, build a minimal agent, then wrap up.",
        sections=[
            ContentPlanSection(
                title="Introduction to AI Agents and Strands",
                coverage_description="Explain agentic AI and Strands as a beginner-friendly SDK.",
                order=0,
            ),
            ContentPlanSection(
                title="Setup and Basic Agent Creation",
                coverage_description="Install steps and a minimal agent code example.",
                order=1,
            ),
        ],
        title_candidates=[
            TitleCandidate(title="Build Your First AI Agent", probability_of_success=0.7)
        ],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    draft_input = WriterInput(
        content_plan=content_plan,
        audience="Beginners to AI Agents",
        tone_or_purpose="Educational",
    )

    result = agent.run(draft_input)
    print("\n--- Draft ---\n")
    print(result.draft)


if __name__ == "__main__":
    main()
