# Decision: Default Structured-Output Pattern for New Agents

## Status

**Decided.** This document states which structured-output mechanism is the
default for a **new** agent (any team, not just `software_engineering_team`)
that needs a typed result back from the LLM, and documents the one existing
format that is a justified exception rather than a candidate for migration.

## Decision

**Default: `generate_structured`**
(`backend/agents/llm_service/api.py`).

```python
from llm_service import generate_structured

result = generate_structured(
    prompt,
    schema=MySchema,
    objective="describe what this call produces",
    system_prompt=system_prompt,
    agent_key="my_agent",
)
```

`generate_structured` returns a Pydantic-typed, schema-validated result. It
enforces JSON mode at the provider level and applies
`llm_service.complete_validated`'s schema-grounded self-correction retry, so
a model response that fails validation gets one corrective retry before the
caller sees a `pydantic.ValidationError` — a hallucinated field or malformed
value becomes a typed exception, not a silent bad parse. See
`backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md` for
the full contract.

A new agent that needs any typed/structured response — a plan, a review
verdict, a list of findings, an answer bounded to a fixed set of IDs — should
call `generate_structured` rather than hand-rolling JSON-mode prompting and
manual parsing, or reaching for one of the legacy entrypoints
(`complete`, `complete_text`, `complete_json`, `chat_json_round`), which
remain supported for existing call sites but are not the recommended
starting point for new code.

This document is about the **structured-output mechanism** (how a typed
response is requested and validated). It is orthogonal to
[`docs/LLM_CALLING_PATTERN_DECISION.md`](LLM_CALLING_PATTERN_DECISION.md),
which decides the surrounding call-site **fallback/retry policy** shape for
`software_engineering_team` tool agents specifically.

## The one documented exception: v2 marker-template format

The `backend_code_v2_team` and `frontend_code_v2_team` workers (see
`backend/agents/software_engineering_team/shared/v2_output_templates.py`)
do not use `generate_structured` for their file-generation output. Instead
they parse a section-delimited plain-text format using fixed markers
(`## FILE <path> ##`, plus summary/microtask/review markers defined in that
module).

This is justified, not grandfathered: these prompts return **full
multi-file source code bodies** as the payload. Requiring that payload to be
valid JSON would mean escaping arbitrary source text (quotes, newlines,
backslashes) into a JSON string, which is exactly the failure mode a
schema-validated structured call is meant to avoid — it trades one parsing
risk for a worse one. The prompt instructions state this directly, e.g.
`backend_code_v2_team/prompts.py`'s `FILES_OUTPUT_TEMPLATE_INSTRUCTIONS`
(mirrored in `frontend_code_v2_team/prompts.py`):

> Use "## FILE \<path\> ##" at the start of each file... Do not use JSON. Use
> only the template above. No explanatory text before or after.

A new agent that returns bulk source-file content in this same shape may
reuse the marker-template format instead of `generate_structured`. Any other
new structured-output need should use `generate_structured`.

## Out of scope

- Migrating `backend_code_v2_team` / `frontend_code_v2_team` (or any other
  existing agent) to `generate_structured`.

## See also

- `backend/agents/llm_service/api.py` — `generate_structured` /
  `generate_text` implementation and module docstring.
- `backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md` —
  full structured-output contract.
- [`docs/LLM_CALLING_PATTERN_DECISION.md`](LLM_CALLING_PATTERN_DECISION.md) —
  canonical call-site fallback/retry pattern (a related but separate
  decision).
