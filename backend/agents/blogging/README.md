# Blogging Agent Suite

This package provides the **blogging agent suite**: research, review, draft, copy-edit, and publication (with optional platform-specific output for Medium, dev.to, and Substack).

## Agents overview

| Agent | Role |
|-------|------|
| **Research** | Brief → web (Ollama web_search) + arXiv search → ranked references, compiled document, notes |
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
- **Draft** writes the initial draft from research + outline. Style and brand content are loaded by the caller before agent creation and passed in as full file contents (see Style guide below).
- **Copy Editor** reviews the draft and returns feedback; the **Draft** agent revises based on feedback. This loop runs a configurable number of times (e.g. 3).
- **Publication** receives the final draft: submit → human approve/reject. On approve: write to `blog_posts/`, generate platform-specific versions. On reject: optional revision loop with Draft + Copy Editor.

**Example scripts:**
- [blogging/agent_implementations/blog_writing_process.py](agent_implementations/blog_writing_process.py) – Full pipeline: research → review → draft → copy-editor loop.
- [blogging/agent_implementations/blog_writing_process_v2.py](agent_implementations/blog_writing_process_v2.py) – Brand-aligned pipeline with artifact persistence and gates.
- [blogging/agent_implementations/run_publication_agent.py](agent_implementations/run_publication_agent.py) – Publication agent (submit, approve, reject, revision loop).

## Brand-aligned pipeline (v2)

When `work_dir` is provided, the pipeline persists all outputs as versioned artifacts and runs hard gates:

**Artifacts** (in `work_dir`):
- `brand_spec_prompt.md` – Brand and style rules (single source of truth)
- `content_brief.md` – Audience model, title choices, outline
- `research_packet.md` – Compiled research document
- `allowed_claims.json` – Evidence-backed factual claims (writer must tag as `[CLAIM:id]`)
- `outline.md` – Blog outline
- `draft_v1.md`, `draft_v2.md`, `final.md` – Draft versions
- `validator_report.json` – Deterministic checks (banned phrases, paragraph length, reading level, etc.)
- `compliance_report.json` – Brand/style violations (PASS/FAIL; FAIL blocks publication)
- `fact_check_report.json` – Claims and risk status
- `publishing_pack.json` – Title options, meta description (when gates PASS)

**Hard gates** (publish-ready only when all PASS):
- Deterministic validators → `validator_report.json`
- Fact-Checker / Risk → claims and risk PASS
- Brand and Style Enforcer → `compliance_report.json` (veto on FAIL)

**Closed-loop rewrite**: On any FAIL, the pipeline passes `required_fixes` to the Draft agent and re-runs gates until PASS or max iterations (default 3). Then status is `NEEDS_HUMAN_REVIEW`.

**API**: `POST /full-pipeline` runs the full pipeline with gates. `POST /research-and-review` accepts optional `work_dir` or `run_id` to persist artifacts.

## Features

- Accepts a short content brief plus optional audience and purpose.
- Generates multiple targeted web search queries from the brief.
- Fetches and skims candidate pages from the public web (Ollama web_search) and arXiv.
- Ranks sources by relevance, authority, recency, and diversity.
- Returns structured references with summaries and key points.
- Exposes models and agent classes that can be wired into a Strands runtime.

## Quick start

1. **Install dependencies**

```bash
pip install -r requirements.txt
```

2. **Configure environment**

- Web search uses Ollama's web_search API; set `OLLAMA_API_KEY` (e.g. from https://ollama.com/settings/keys) for the research agent.

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

Optional: `work_dir` or `run_id` – when set, persists `research_packet.md` and `outline.md` to the artifact directory.

**POST `/full-pipeline`** – Run the full brand-aligned pipeline (research → review → draft → validators → compliance → rewrite loop). Persists all artifacts. Returns `status` (PASS, FAIL, NEEDS_HUMAN_REVIEW), `work_dir`, and draft preview.

**GET `/health`** – Health check.

Interactive docs: http://localhost:8000/docs

## Style guide and brand spec

The Draft and Copy Editor agents do **not** accept file paths. Callers must load the writing style guide and brand spec **before** instantiating the agents, then pass the **full file contents** as strings:

- **Writing style guide**: typically `docs/writing_guidelines.md` (read as UTF-8 text).
- **Brand spec prompt**: typically `docs/brand_spec_prompt.md` (read as full text via `load_brand_spec_prompt` for draft/editor and compliance; validators use a default in-memory spec).

Use `shared.load_style_file(path, label)` to load a file: on success it returns the stripped content; on failure (missing file, read error) it **logs an error** and returns an empty string. Then instantiate the agents with `writing_style_guide_content=...` and `brand_spec_content=...`. If both contents are empty, the agents use a minimal built-in fallback.

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
│       ├── web_search.py    # Ollama web_search
│       ├── web_fetch.py     # Web fetch/scrape
│       └── arxiv_search.py # arXiv search
├── blog_review_agent/       # Title choices + outline
├── blog_draft_agent/        # Draft from research + outline; revise from feedback
├── blog_copy_editor_agent/  # Draft → feedback items
├── blog_compliance_agent/   # Brand/style enforcer (veto on FAIL)
├── blog_fact_check_agent/   # Claims and risk officer
├── blog_publication_agent/  # Submit, approve/reject, platform versions
├── shared/                  # Artifacts, brand_spec_prompt loader
├── validators/              # Deterministic checks (banned phrases, reading level, etc.)
├── agent_implementations/
│   ├── run_api_server.py
│   ├── blog_writing_process.py
│   ├── blog_writing_process_v2.py   # Brand-aligned pipeline with gates
│   └── ...
├── docs/
│   ├── writing_guidelines.md
│   └── brand_spec_prompt.md
└── requirements.txt
```

## License

This repository is provided as an example implementation for building Strands-style research agents.
