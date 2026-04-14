# Blog Research & Planning API

HTTP API exposing **research + planning** from the [blogging](../blogging/) agent suite. Produces title choices and an outline from a content brief via a persisted **ContentPlan** (same planning step as the full pipeline).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/research-and-review` | Run research and planning; returns title choices, outline, compiled document |
| GET | `/health` | Health check |

## How to Run

From the repository root:

```bash
pip install -r requirements.txt
PYTHONPATH=blogging uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Interactive docs: http://localhost:8000/docs

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OLLAMA_API_KEY` | Ollama API key; used for LLM and for blogging research web search (Ollama web_search API). |

## Request Example

**POST `/research-and-review`**

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

`audience` can be an object (`skill_level`, `profession`, `hobbies`, `other`) or a free-text string. `title_concept` and `audience` are optional. Optional `content_profile`, `series_context`, `length_notes`, and `target_word_count` match the full pipeline (see blogging OpenAPI / README).

## Response

- `title_choices` – Title options with probability of success (from planning)
- `outline` – Outline derived from the **ContentPlan** (Markdown)
- `compiled_document` – Formatted research (sources, papers, similar topics)
- `notes` – High-level synthesis and suggestions

## Full Blogging Pipeline

This mount may expose **research + planning** only, depending on deployment. For the full pipeline (planning → draft → copy-editor → gates → publication), see [blogging/README.md](../blogging/README.md) and the blogging service’s `/full-pipeline` routes.

## Khala platform

This package is part of the [Khala](../../../README.md) monorepo (Unified API, Angular UI, and full team index).
