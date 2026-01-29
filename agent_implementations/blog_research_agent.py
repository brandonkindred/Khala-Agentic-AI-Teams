import logging

from strands_research_agent.agent import ResearchAgent
from strands_research_agent.models import ResearchBriefInput
from strands_research_agent.llm import OllamaLLMClient  # or your own LLM client

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

llm_client = OllamaLLMClient(  # points at local Ollama (127.0.0.1:11434) by default
    model="deepseek-r1",  # change to your preferred Ollama model
)
agent = ResearchAgent(llm_client=llm_client)

brief = ResearchBriefInput(
    brief="LLM observability best practices for large enterprises",
    audience="CTOs and platform teams",
    tone_or_purpose="technical deep-dive",
    max_results=8,
)

result = agent.run(brief)

for ref in result.references:
    print(ref.title, ref.url)