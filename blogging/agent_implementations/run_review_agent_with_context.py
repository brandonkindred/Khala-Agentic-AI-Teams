"""
Test the blog review agent using fixed context (synthesis text from research).

Uses the provided text as the brief so you can get title choices and outline
without running the full research pipeline.
"""

import _path_setup  # noqa: F401  # Add blogging to path when run from project root

import logging

from blog_research_agent.llm import OllamaLLMClient  # or DummyLLMClient for quick test
from blog_research_agent.models import ResearchReference
from blog_review_agent import BlogReviewAgent, BlogReviewInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Context text: synthesis from research agent (what the post is about)
CONTEXT_BRIEF = """The main themes across references emphasize Strands Agents as a model-driven, open-source SDK that simplifies AI agent development by reducing boilerplate and enabling dynamic orchestration through large language models (LLMs). Consensus includes its user-friendly design for beginners, rapid deployment capabilities, integration with AWS services, and educational focus, with all sources agreeing on its lightweight and production-ready nature. Disagreements or controversies are minimal, but a potential trade-off exists between the flexibility of the model-driven approach and reduced control over predefined workflows. Gaps include limited coverage of advanced multi-agent patterns, comparisons with other frameworks, and specific applications like creative blog writing, where more research could explore optimization for iterative tasks or non-AWS integrations.

Suggested outline:
- Introduction to AI Agents and Strands: Explain the concept of agentic AI and introduce Strands as a beginner-friendly SDK.
- Setup and Installation: Provide step-by-step instructions for installing Strands, including code snippets for local development.
- Basic Agent Creation: Demonstrate how to build a simple AI agent using minimal code, focusing on handling user prompts for blog topics.
- Customizing the Agent: Show how to add system prompts and customize the agent for blog writing tasks, such as generating content outlines.
- Integrating Tools: Include examples of integrating external tools (e.g., APIs for research) to enhance the agent's capabilities in writing full blog posts.
- Running and Testing: Offer guidance on executing the agent, testing outputs, and debugging common issues with code samples.
- Deployment Options: Discuss deploying the agent to cloud environments like AWS, with code for production use.
- Conclusion and Resources: Summarize key learnings, suggest further reading, and encourage community engagement on GitHub."""

# Minimal placeholder reference so the review agent has something to anchor on
PLACEHOLDER_REF = ResearchReference(
    title="Strands Agents SDK – overview",
    url="https://github.com/strands-agents/strands",
    summary="Strands Agents is a model-driven, open-source SDK for building AI agents with LLMs; lightweight and production-ready.",
    key_points=[
        "Beginner-friendly, reduces boilerplate",
        "Integration with AWS",
        "Dynamic orchestration via LLMs",
    ],
)

if __name__ == "__main__":
    llm_client = OllamaLLMClient(
        model="deepseek-r1",
        timeout=600.0,
    )
    # Or use DummyLLMClient for a quick run without Ollama:
    # from blog_research_agent.llm import DummyLLMClient
    # llm_client = DummyLLMClient()

    review_agent = BlogReviewAgent(llm_client=llm_client)
    review_input = BlogReviewInput(
        brief=CONTEXT_BRIEF,
        audience="Beginners to AI Agents",
        tone_or_purpose="Educational",
        references=[PLACEHOLDER_REF],
    )
    review_result = review_agent.run(review_input)

    print("\n--- Top 10 title choices (with probability of success) ---")
    for i, tc in enumerate(review_result.title_choices, 1):
        print(f"{i}. {tc.title}  [{tc.probability_of_success:.0%}]")
    print("\n--- Blog outline ---\n")
    print(review_result.outline)
