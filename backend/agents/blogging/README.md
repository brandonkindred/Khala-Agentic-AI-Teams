# Blogging Agent Suite

This package provides the **blogging agent suite**: research, **planning**, writer, copy-edit, and publication (with optional platform-specific output for Medium, dev.to, and Substack).

## Agents overview

| Agent | Role |
|-------|------|
| **Research** | Brief → web (Ollama web_search) + arXiv search → ranked references, compiled document, notes |
| **Planning** | Research digest + length profile → structured **content plan** (titles, narrative flow, per-section coverage, requirements analysis) with refine-until-done |
| **Writer** | Research document + **content plan** + style guide → draft; supports revise-from-feedback (e.g. from Copy Editor) |
| **Copy Editor** | Draft → feedback items and summary (for Writer revision loop); optional **content plan** context for structure-aware feedback |
| **Publication** | Submit draft → pending; human approve → write to `blog_posts`, generate Medium/dev.to/Substack versions; reject → optional revision loop with Writer + Copy Editor |
| **Medium stats** | Playwright automation using the **Medium.com platform integration** (stored browser session) → scrape [medium.com/me/stats](https://medium.com/me/stats) → `medium_stats_report.json` artifact |

### Medium statistics agent (use at your own risk)

Automating a logged-in Medium session may conflict with Medium’s terms of service. Intended for **internal** use only. The UI changes often; selectors may need updates.

**Install browser binaries** (once per environment):

```bash
pip install -r requirements.txt
playwright install chromium
```

**Auth:** The agent runs when the **Medium** integration is **enabled** and a Playwright `storage_state` exists on disk (or **shared** Google browser credentials are saved in Postgres — **`PUT /api/integrations/google-browser-login`** with `POSTGRES_HOST` set — so the resolver can log in automatically). With provider **Google**, use **`POST /api/integrations/medium/session/browser-login`** or run stats (auto-login if session is missing). Optional platform Google OAuth (`/medium/oauth/google/*`) is separate.

**API:** `POST /medium-stats` (sync) and `POST /medium-stats-async` (job + poll `GET /job/{id}`). Without a valid integration, these return **503**. Results are written to `medium_stats_report.json` in the job’s `work_dir` and appear in the blogging dashboard artifact list.

Optional env: `BLOGGING_MEDIUM_STATS_ROOT` (job work dir base); Google redirect override `MEDIUM_GOOGLE_REDIRECT_URI` — see root `CLAUDE.md`.

## Full pipeline

```
Research → Planning → Writer → (optional) Writer ↔ Copy Editor revision loop → (optional) Publication
```

- **Research** fetches and ranks sources from the web and arXiv.
- **Planning** produces a persisted content plan (`content_plan.json` / `content_plan.md`): titles, narrative flow, section coverage, and analysis; refine loop until the plan is acceptable for the profile.
- **Writer** writes the initial draft from research + **content plan**. Style and brand content are loaded by the caller before agent creation and passed in as full file contents (see Style guide below).
- **Copy Editor** reviews the draft and returns feedback; the **Writer** agent revises based on feedback. In the v2 pipeline this loop runs up to `DRAFT_EDITOR_ITERATIONS` times (default 500; stops early when the editor approves).
- **Publication** receives the final draft: submit → human approve/reject. On approve: write to `blog_posts/`, generate platform-specific versions. On reject: optional revision loop with Writer + Copy Editor.

**Example scripts:**
- [blogging/agent_implementations/blog_writing_process.py](agent_implementations/blog_writing_process.py) – Legacy pipeline (superseded by v2 for production).
- [blogging/agent_implementations/blog_writing_process_v2.py](agent_implementations/blog_writing_process_v2.py) – Brand-aligned pipeline with artifact persistence and gates.
- [blogging/agent_implementations/run_publication_agent.py](agent_implementations/run_publication_agent.py) – Publication agent (submit, approve, reject, revision loop).

## Brand-aligned pipeline (v2)

When `work_dir` is provided, the pipeline persists all outputs as versioned artifacts and runs hard gates:

**Artifacts** (in `work_dir`):
- `brand_spec_prompt.md` – Brand and style rules (single source of truth)
- `content_brief.md` – Audience model, title choices, outline (derived from content plan)
- `content_plan.json` / `content_plan.md` – Structured plan + requirements analysis
- `research_packet.md` – Compiled research document
- `allowed_claims.json` – Evidence-backed factual claims (writer must tag as `[CLAIM:id]`)
- `outline.md` – Flat outline derived from the content plan (for display / compatibility)
- `draft_v1.md`, `draft_v2.md`, `final.md` – Draft versions
- `validator_report.json` – Deterministic checks (banned phrases, paragraph length, reading level, etc.)
- `compliance_report.json` – Brand/style violations (PASS/FAIL; FAIL blocks publication)
- `fact_check_report.json` – Claims and risk status
- `publishing_pack.json` – Title options, meta description (when gates PASS)

**Hard gates** (publish-ready only when all PASS):
- Deterministic validators → `validator_report.json`
- Fact-Checker / Risk → claims and risk PASS
- Brand and Style Enforcer → `compliance_report.json` (veto on FAIL)

**Closed-loop rewrite**: On any FAIL, the pipeline passes `required_fixes` to the Writer agent and re-runs gates until PASS or max iterations (default 3). Then status is `NEEDS_HUMAN_REVIEW`.

**API**: `POST /full-pipeline` runs the full pipeline with gates. `POST /research-and-review` runs **research + planning** (same planning step as the full pipeline) and accepts optional `work_dir` or `run_id` to persist artifacts.

**Env (planning):** `BLOG_PLANNING_MAX_ITERATIONS` (default 5), `BLOG_PLANNING_MAX_PARSE_RETRIES` (default 3), optional `BLOG_PLANNING_MODEL` (Ollama model name for planning only; same API base as `LLM_*`).

### Planning definition of done (refine loop)

Refinement stops and the pipeline proceeds **only when** `requirements_analysis.plan_acceptable` **and** `requirements_analysis.scope_feasible` are both true (see `RequirementsAnalysis` in `shared/content_plan.py`). Post-validation may set `plan_acceptable` false when the section count is outside `[min,max]` for the chosen `content_profile`.

**`planning_failure_reason`** (enum `PlanningFailureReason`) when planning stops without an acceptable plan:

| Value | Meaning |
|-------|---------|
| `max_iterations_reached` | Refine loop exhausted `BLOG_PLANNING_MAX_ITERATIONS` |
| `infeasible_scope` | Reserved for explicit scope aborts |
| `parse_failure` | JSON schema/parse failed after bounded retries |
| `model_abort` | Reserved |

HTTP: failed planning returns **422** on sync `/full-pipeline` with `detail.failure_reason` when available; async jobs store `failed_phase=planning` and optional `planning_failure_reason` on the job record.

**Breaking change:** The old `BlogReviewAgent` package has been removed; titles and outline always come from the planning phase / `ContentPlan`.

### Content profiles (guideline-based length)

Full-pipeline requests can set a **`content_profile`** instead of guessing a word count:

| Profile | Typical target | Use case |
|---------|----------------|----------|
| `short_listicle` | ~750 words | Scannable listicle / high-level explainer |
| `standard_article` | ~1000 words | Default balanced article |
| `technical_deep_dive` | ~2200 words | Substantive technical detail |
| `series_instalment` | ~1400 words | One post in a multi-part series |

Optional **`series_context`** scopes outlines and drafts to a single instalment. **`length_notes`** adds author-specific scope hints.

**Precedence:** If **`target_word_count`** is sent, it overrides the numeric target (still clamped 100–10_000); soft bands scale from it. The profile still influences editor strictness (e.g. tighter over-length checks for deep dives, looser for listicles). If both profile and target are omitted, behavior matches the legacy default (`standard_article`, ~1000 words).

Resolution lives in [`shared/content_profile.py`](shared/content_profile.py).

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

The Writer and Copy Editor agents do **not** accept file paths. Callers must load the writing style guide and brand spec **before** instantiating the agents, then pass the **full file contents** as strings:

- **Writing style guide**: typically `docs/writing_guidelines.md` (a Jinja2 template; rendered against the configured author profile when loaded).
- **Brand spec prompt**: typically `docs/brand_spec_prompt.md` (a Jinja2 template; rendered via `load_brand_spec_prompt` for writer/editor and compliance; validators use a default in-memory spec).

Both templates pull the user's identity, voice, and background from an `AuthorProfile` resolved at runtime — see `author_profile/` and the `AUTHOR_PROFILE_PATH` / `AUTHOR_PROFILE_STRICT` env vars in the root `CLAUDE.md`. To customize the author voice without editing the templates, copy `author_profile/author_profile.example.yaml`, fill it in, and either set `AUTHOR_PROFILE_PATH` or drop the file at `$AGENT_CACHE/author_profile.yaml`.

Use `shared.load_style_file(path, label)` to load a file: on success it returns the rendered, stripped content; on failure (missing file, read error, render error) it **logs an error** and returns an empty string. Then instantiate the agents with `writing_style_guide_content=...` and `brand_spec_content=...`. If both contents are empty, the agents use a minimal built-in fallback.

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
├── blog_planning_agent/     # Structured content plan + refine loop
├── blog_writer_agent/       # Writer: draft from research + content plan; revise from feedback
├── blog_copy_editor_agent/  # Draft → feedback items
├── blog_compliance_agent/   # Brand/style enforcer (veto on FAIL)
├── blog_fact_check_agent/   # Claims and risk officer
├── blog_publication_agent/  # Submit, approve/reject, platform versions
├── blog_medium_stats_agent/ # Medium dashboard stats (Playwright)
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

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
