# Changelog

All notable changes to this repository are documented here.

## [Unreleased]

### Added

- **`llm_service` structured-output contract:** new top-level `generate_text` / `generate_structured` entrypoints and a `complete_validated` helper that layers Pydantic validation + one schema-grounded self-correction retry on top of `complete_json`. New `LLMSchemaValidationError` carries `correction_attempts_used`; `LLMJsonParseError` gained the same optional field. A CI static check (`agents/llm_service/tests/test_no_markdown_in_structured.py`) prevents `*_PROMPT` constants with Markdown/prose bodies from being routed into JSON-only methods. Legacy `complete` / `complete_text` / `complete_json` / `chat_json_round` are unchanged and remain supported. See [FEATURE_SPEC_structured_output_contract.md](backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md).

### Fixed

- **`user_agent_founder` spec generation:** the "Startup Founder Testing Persona" run no longer fails with `LLMJsonParseError` when the model returns Markdown. `FounderAgent.generate_spec` now bypasses the Strands / `chat_json_round` JSON-only transport and calls `LLMClient.complete` directly via a new `_call_text` helper. `FounderAgent.answer_question` migrated to `generate_structured` with a `FounderAnswer` Pydantic schema (canary for the new API); the bespoke regex/`json.loads` fallback was removed in favor of the self-correction guard.
- **Ollama structured JSON:** responses with unescaped double quotes inside string values (common when the model cites snippets like `"Resource": "*"`) are parsed using a **`json-repair`** fallback in `OllamaLLMClient._extract_json`. Blog copy-editor prompt updated to require escaped or single-quoted inner quotes.

### Breaking changes

- **Blogging pipeline:** `BlogReviewAgent` has been removed. The pipeline is **research → planning → writer** with a persisted `ContentPlan` (`content_plan.json` / `content_plan.md`). `POST /research-and-review` (sync and async) runs the same **research + planning** step as the full pipeline, not a separate “review” agent. `POST /full-pipeline` returns `title_choices` and `outline` derived from the approved plan; planning failure returns **422** with `planning_failed` detail. Async jobs expose a **planning** phase and optional planning observability fields on completed jobs; failed planning jobs may include `planning_failure_reason`. Optional env: `BLOG_PLANNING_MODEL`, `BLOG_PLANNING_MAX_ITERATIONS`, `BLOG_PLANNING_MAX_PARSE_RETRIES`.
