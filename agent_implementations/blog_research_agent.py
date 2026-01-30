import logging

from strands_research_agent.agent import ResearchAgent
from strands_research_agent.agent_cache import AgentCache
from strands_research_agent.models import ResearchBriefInput
from strands_research_agent.llm import OllamaLLMClient  # or your own LLM client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

llm_client = OllamaLLMClient(
    model="deepseek-r1",  # change to your preferred Ollama model
    timeout=600.0,  # per-request timeout; scoring/summarization can be slow (raise if ReadTimeout)
)

# Enable caching for checkpoint/resume capability
cache = AgentCache(cache_dir=".agent_cache")
agent = ResearchAgent(llm_client=llm_client, cache=cache)

brief = ResearchBriefInput(
    brief="LLM observability best practices for large enterprises",
    audience="CTOs and platform teams",
    tone_or_purpose="technical deep-dive",
    max_results=8,
)

result = agent.run(brief)

for ref in result.references:
    print(ref.title, ref.url)