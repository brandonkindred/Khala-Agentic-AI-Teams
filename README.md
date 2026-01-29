# Strands Research Agent

This repository provides a Python-based **Strands-style research agent** that takes a short content brief, performs web research, and returns a curated list of **relevant, high-quality references** with summaries and key points.

The agent is designed to be embedded into a Strands agents environment or any Python application that needs structured research results based on a high-level brief.

## Features

- Accepts a short content brief plus optional audience and purpose.
- Generates multiple targeted web search queries from the brief.
- Fetches and skims candidate pages from the public web.
- Ranks sources by relevance, authority, recency, and diversity.
- Returns structured references with summaries and key points.
- Exposes models and a `ResearchAgent` class that can be wired into a Strands runtime.

## Quick start

1. **Install dependencies**

```bash
pip install -r requirements.txt
```

2. **Configure environment**

- Set a `TAVILY_API_KEY` environment variable (or adapt the `web_search` tool to your preferred search API).

3. **Use the agent in Python**

```python
from strands_research_agent.agent import ResearchAgent
from strands_research_agent.models import ResearchBriefInput
from strands_research_agent.llm import OllamaLLMClient  # or your own LLM client

llm_client = OllamaLLMClient(  # points at local Ollama (127.0.0.1:11434) by default
    model="llama3.1",  # change to your preferred Ollama model
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
```

## Project layout

- `strands_research_agent/models.py` – Input/output models and internal data structures.
- `strands_research_agent/tools/web_search.py` – Web search tool wrapper.
- `strands_research_agent/tools/web_fetch.py` – Web fetch/scrape tool.
- `strands_research_agent/prompts.py` – Prompt templates for LLM calls.
- `strands_research_agent/agent.py` – Core research agent implementation.
- `strands_research_agent/strands_integration.py` – Helper for wiring into a Strands runtime.

## License

This repository is provided as an example implementation for building Strands-style research agents.

