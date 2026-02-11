import _path_setup  # noqa: F401  # Add blogging to path when run from project root

import logging

from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.models import ResearchBriefInput
from blog_research_agent.llm import OllamaLLMClient  # or your own LLM client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

llm_client = OllamaLLMClient(
    model="deepseek-r1",  # change to your preferred Ollama model
    timeout=600.0,  # per-request timeout; scoring/summarization can be slow (raise if ReadTimeout)
)

# Enable caching for checkpoint/resume capability
cache = AgentCache(cache_dir=".agent_cache")
agent = ResearchAgent(llm_client=llm_client, cache=cache)

brief = ResearchBriefInput(
    brief="Building an AI Agent with Strands. A step by step guide to building an AI Agent with Strands. The blog should be a guide for beginners to understand the concepts of AI Agents and how to build them with Strands. The blog post should include code that the user can copy and paste to follow along and build the agent themselves. The AI agent that the blog post should walk the reader through creating an agent with a simple prompt for writing a blog posts.",
    audience="Beginners to AI Agents",
    tone_or_purpose="Educational",
    max_results=20,
)
result = agent.run(brief)

print(result.notes)
