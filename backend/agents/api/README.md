# Blog Research & Review API

HTTP API exposing the **research + review** pipeline from the [blogging](../blogging/) agent suite. Produces title choices and a blog outline from a content brief.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/research-and-review` | Run research and review agents; returns title choices, outline, compiled document |
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
| `TAVILY_API_KEY` | API key for web search (Tavily). Required for research agent. |

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

`audience` can be an object (`skill_level`, `profession`, `hobbies`, `other`) or a free-text string. `title_concept` and `audience` are optional.

## Response

- `title_choices` – Top title options with probability of success
- `outline` – Detailed blog outline with notes for the first draft
- `compiled_document` – Formatted research (sources, papers, similar topics)
- `notes` – High-level synthesis and suggestions

## Full Blogging Pipeline

This API exposes **research + review** only. For the full pipeline (draft, copy-editor loop, publication), see [blogging/README.md](../blogging/README.md).
