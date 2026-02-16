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
from blog_research_agent.agent import ResearchAgent
from blog_research_agent.models import ResearchBriefInput
from blog_research_agent.llm import OllamaLLMClient  # or your own LLM client

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

## Logging

The research agent logs progress to the `blog_research_agent.agent` logger so you can see what it is doing at each step (parsing brief, generating queries, running searches, fetching and scoring documents, summarizing references, synthesizing the overview). By default the library does not configure handlers; enable logging in your application to see output.

**Console (INFO):**

```python
import logging
logging.basicConfig(level=logging.INFO)
# then run your agent
```

**Target the agent logger only (e.g. INFO or DEBUG):**

```python
import logging
logging.getLogger("blog_research_agent.agent").setLevel(logging.INFO)
# add a handler if the root logger has none
logging.getLogger("blog_research_agent.agent").addHandler(logging.StreamHandler())
```

## Project layout

Each agent lives in its own folder with its supporting code and resources.

**Research agent** (`blog_research_agent/`):

- `blog_research_agent/models.py` – Input/output models and internal data structures.
- `blog_research_agent/tools/web_search.py` – Web search tool wrapper.
- `blog_research_agent/tools/web_fetch.py` – Web fetch/scrape tool.
- `blog_research_agent/prompts.py` – Prompt templates for LLM calls.
- `blog_research_agent/agent.py` – Core research agent implementation.
- `blog_research_agent/strands_integration.py` – Helper for wiring into a Strands runtime.
- `blog_research_agent/agent_cache.py` – Checkpoint/resume cache for the research agent.

**Review agent** (`blog_review_agent/`):

- `blog_review_agent/agent.py` – Blog review agent (title choices + outline from brief + sources).
- `blog_review_agent/models.py` – TitleChoice, BlogReviewInput, BlogReviewOutput.
- `blog_review_agent/prompts.py` – Prompt for titles and outline.

**Draft agent** (`blog_draft_agent/`):

- `blog_draft_agent/agent.py` – Blog draft agent (draft from research document + outline, compliant with a style guide).
- `blog_draft_agent/models.py` – DraftInput, DraftOutput.
- `blog_draft_agent/prompts.py` – Prompt for draft generation. Use `docs/brandon_kindred_brand_and_writing_style_guide.md` as the style guide.

## API

A FastAPI server exposes the research-and-review pipeline as an HTTP endpoint.

**Start the server:**

```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
# or
python3 agent_implementations/run_api_server.py
```

**POST `/research-and-review`** – Run research and review agents.

Request body:

```json
{
  "brief": "LLM observability best practices for large enterprises",
  "title_concept": "Why CTOs need it",
  "audience": {
    "skill_level": "expert",
    "profession": "CTO",
    "hobbies": ["AI", "DevOps"]
  },
  "tone_or_purpose": "technical deep-dive",
  "max_results": 20
}
```

`audience` can be an object (skill_level, profession, hobbies, other) or a free-text string. `title_concept` and `audience` are optional.

Response: `title_choices`, `outline`, `compiled_document`, `notes`.

**GET `/health`** – Health check.

Interactive docs: http://localhost:8000/docs

## License

This repository is provided as an example implementation for building Strands-style research agents.
## Additional teams

- `social_media_marketing_team/` – Multi-agent social marketing workflow with platform specialists (LinkedIn, Facebook, Instagram, X), proposal collaboration with orchestrator consensus, human approval gate, and 14-day cadence planning defaults.

