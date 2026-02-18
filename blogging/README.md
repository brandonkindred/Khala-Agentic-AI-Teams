# Blogging Agent Suite

This package provides the **blogging agent suite**: research, review, draft, copy-edit, and publication (with optional platform-specific output for Medium, dev.to, and Substack).

## Agents overview

| Agent | Role |
|-------|------|
| **Research** | Brief → web (Tavily) + arXiv search → ranked references, compiled document, notes |
| **Review** | Brief + references → title choices + outline |
| **Draft** | Research document + outline + style guide → draft; supports revise-from-feedback (e.g. from Copy Editor) |
| **Copy Editor** | Draft → feedback items and summary (for Draft revision loop) |
| **Publication** | Submit draft → pending; human approve → write to `blog_posts`, generate Medium/dev.to/Substack versions; reject → optional revision loop with Draft + Copy Editor |

## Full pipeline

```
Research → Review → Draft → (optional) Draft ↔ Copy Editor revision loop → (optional) Publication
```

- **Research** fetches and ranks sources from the web and arXiv.
- **Review** produces title choices and a detailed outline.
- **Draft** writes the initial draft from research + outline; uses `docs/brandon_kindred_brand_and_writing_style_guide.md` as the style guide.
- **Copy Editor** reviews the draft and returns feedback; the **Draft** agent revises based on feedback. This loop runs a configurable number of times (e.g. 3).
- **Publication** receives the final draft: submit → human approve/reject. On approve: write to `blog_posts/`, generate platform-specific versions. On reject: optional revision loop with Draft + Copy Editor.

**Example scripts:**
- [blogging/agent_implementations/blog_writing_process.py](agent_implementations/blog_writing_process.py) – Full pipeline: research → review → draft → copy-editor loop.
- [blogging/agent_implementations/run_publication_agent.py](agent_implementations/run_publication_agent.py) – Publication agent (submit, approve, reject, revision loop).

## Features

- Accepts a short content brief plus optional audience and purpose.
- Generates multiple targeted web search queries from the brief.
- Fetches and skims candidate pages from the public web (Tavily) and arXiv.
- Ranks sources by relevance, authority, recency, and diversity.
- Returns structured references with summaries and key points.
- Exposes models and agent classes that can be wired into a Strands runtime.

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

## Run the API

Run from the **blogging** directory (or from repo root with `PYTHONPATH=blogging` if using root `api.main`):

```bash
cd blogging
python agent_implementations/run_api_server.py
# or
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
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

## Style guide

The Draft and Copy Editor agents use `docs/brandon_kindred_brand_and_writing_style_guide.md` as the style guide. Pass a custom path via `default_style_guide_path` when instantiating the agents.

## Logging

The research agent logs progress to the `blog_research_agent.agent` logger. Enable logging in your application to see output:

```python
import logging
logging.basicConfig(level=logging.INFO)
# then run your agent
```

Target the agent logger only:

```python
logging.getLogger("blog_research_agent.agent").setLevel(logging.INFO)
logging.getLogger("blog_research_agent.agent").addHandler(logging.StreamHandler())
```

## Project layout

```
blogging/
├── api/
│   └── main.py              # FastAPI app (research-and-review endpoint)
├── blog_research_agent/     # Web + arXiv research
│   ├── agent.py
│   ├── models.py
│   ├── prompts.py
│   ├── agent_cache.py
│   ├── strands_integration.py
│   └── tools/
│       ├── web_search.py    # Tavily web search
│       ├── web_fetch.py     # Web fetch/scrape
│       └── arxiv_search.py # arXiv search
├── blog_review_agent/       # Title choices + outline
├── blog_draft_agent/        # Draft from research + outline; revise from feedback
├── blog_copy_editor_agent/  # Draft → feedback items
├── blog_publication_agent/  # Submit, approve/reject, platform versions
├── agent_implementations/
│   ├── run_api_server.py
│   ├── run_research_agent.py
│   ├── run_draft_agent.py
│   ├── run_copy_editor_agent.py
│   ├── run_publication_agent.py
│   └── blog_writing_process.py   # Full pipeline example
├── docs/
│   └── brandon_kindred_brand_and_writing_style_guide.md
└── requirements.txt
```

## License

This repository is provided as an example implementation for building Strands-style research agents.
