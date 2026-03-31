# Changelog

All notable changes to this repository are documented here.

## [Unreleased]

### Fixed

- **Ollama structured JSON:** responses with unescaped double quotes inside string values (common when the model cites snippets like `"Resource": "*"`) are parsed using a **`json-repair`** fallback in `OllamaLLMClient._extract_json`. Blog copy-editor prompt updated to require escaped or single-quoted inner quotes.

### Breaking changes

- **Blogging pipeline:** `BlogReviewAgent` has been removed. The pipeline is **research → planning → writer** with a persisted `ContentPlan` (`content_plan.json` / `content_plan.md`). `POST /research-and-review` (sync and async) runs the same **research + planning** step as the full pipeline, not a separate “review” agent. `POST /full-pipeline` returns `title_choices` and `outline` derived from the approved plan; planning failure returns **422** with `planning_failed` detail. Async jobs expose a **planning** phase and optional planning observability fields on completed jobs; failed planning jobs may include `planning_failure_reason`. Optional env: `BLOG_PLANNING_MODEL`, `BLOG_PLANNING_MAX_ITERATIONS`, `BLOG_PLANNING_MAX_PARSE_RETRIES`.
