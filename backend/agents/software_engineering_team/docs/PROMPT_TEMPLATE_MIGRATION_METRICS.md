# Prompt-Template Migration Metrics

Before/after line-count report for the effort to move hand-written prompt
scaffolding onto the reusable builders in `backend/shared/prompts/templates.py`
(`build_json_output_prompt`, `build_document_rewrite_prompt`,
`format_context_block`).

## Investment: shared builders

`backend/shared/prompts/templates.py` gained **~110 lines** to implement the
three reusable builders (the remainder of that change's diff is `__init__.py`
re-exports and a new test file).

## Per-module results

| File | Scaffolding lines removed | Builder-call lines added | Net change |
|---|---|---|---|
| `product_requirements_analysis_agent/prompts.py` | ~46 (23 hand-typed `---`/`---` context fences) | ~41 (23 `format_context_block` + 17 `build_json_output_prompt`/`build_document_rewrite_prompt` calls + import) | −5 |
| `qa_agent/prompts.py` | ~3 (`Output format:` headers + `JSON_OUTPUT_INSTRUCTION` concatenation) | ~17 (2 builder calls + import) | +14 |
| `security_agent/prompts.py` | ~2 | ~8 | +6 |
| `devops_team/cicd_pipeline_agent/prompts.py` | ~2 | ~10 | +8 |
| `devops_team/deployment_strategy_agent/prompts.py` | ~2 | ~8 | +6 |
| `devops_team/doc_runbook_agent/prompts.py` | ~2 | ~8 | +6 |
| `devops_team/iac_agent/prompts.py` | ~2 | ~9 | +7 |
| `devops_team/infra_debug_agent/prompts.py` | ~1 | ~9 | +8 |
| `devops_team/infra_patch_agent/prompts.py` | ~1 | ~9 | +8 |
| `devops_team/task_clarifier/prompts.py` | ~2 | ~9 | +7 |
| **Total** | **~63** | **~128** | **~+65** |

**Not migrated** (docstring/comment-only changes, out of scope for these
totals): `tech_lead_agent/prompts.py` and
`devops_team/devsecops_review_agent/prompts.py`.

## Reading the numbers

Raw line count across the migrated modules went up, not down: Python's
explicit named-parameter call syntax (`role_sentence=`, `json_schema=`,
`trailer=`) is more verbose than the inline string blocks it replaces. The
DRY payoff is qualitative, not a line-count win — roughly 17 independently
worded "respond with JSON only" instructions and 23 hand-typed context
fences now derive from one canonical implementation, so a future change to
either convention touches `templates.py` once instead of being hand-edited
across 11+ files.

## Methodology

Diffs for each migration were read line by line. A removed line counted as
"scaffolding removed" only when a previously hand-authored structural phrase
(`Output format:`, `Return JSON only.`, the `JSON_OUTPUT_INSTRUCTION`
concatenation, or a `---\n{slot}\n---` context fence) disappeared outright
rather than being relocated into a builder's parameter value. "Builder-call
lines added" counts the import plus the structural call/parameter-wrapper
lines (`build_json_output_prompt(`, `role_sentence=`, `json_schema=`,
`trailer=`, `format_context_block(`, closing parens) — not the restated
prose passed into them. New Design-by-Contract docstrings added alongside
some of these migrations are excluded from both columns; they're unrelated
to scaffolding deduplication. Figures are rough (±a few lines per file), not
exact.
