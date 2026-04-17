# Feature Spec: Structured-Output Contract for LLM Calls

## Context

A `user_agent_founder` "Startup Founder Testing Persona" run failed with:

> `LLMJsonParseError: Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. Response preview: '# TaskFlow MVP Product Specification\n**Version:** 0.1 (MVP)...'`

The model did exactly what it was asked to do. The bug is a **broken contract between two sides of the same call**:

- [`agent.py:70`](backend/agents/user_agent_founder/agent.py:70) — `SPEC_GENERATION_PROMPT` instructs the LLM: *"Write the spec as a markdown document with these sections..."*
- [`graphs/lifecycle_graph.py:30`](backend/agents/user_agent_founder/graphs/lifecycle_graph.py:30) — the `generate_spec` graph node's system prompt says: *"Return structured JSON with the specification."*
- The Markdown output then reaches [`extract_json_from_response`](backend/agents/llm_service/util.py:131), which has no Markdown fallback and raises `LLMJsonParseError`.

The same failure mode is referenced by multiple existing remediation plans under `backend/agents/plans/` (e.g. `improve_se_team_reliability_*.plan.md`, `resolve_run_log_warnings_*.plan.md`), confirming it is recurring and cross-team — not specific to the founder persona.

This spec defines a single, coherent fix bundling three interlocking solutions:

1. **Align the founder spec contract** so it is Markdown end-to-end (eliminate this instance).
2. **Add a structured-output guard with one self-correction retry** in `llm_service` (recover the class).
3. **Split the LLM API into `generate_text` vs `generate_structured` call paths** (prevent recurrence by construction).

## Goals & Non-Goals

**Goals**

- The founder "generate spec" path no longer routes Markdown into a JSON parser. Persona runs that produced this `LLMJsonParseError` succeed without changes to the prompt's intent.
- Any caller that genuinely wants JSON gets one automatic, schema-grounded re-ask before failure, reducing single-shot `LLMJsonParseError` rates by ≥80% on the existing test corpus.
- The `llm_service` public surface makes "I want free text" vs "I want a typed object" an explicit, unambiguous choice at the call site. New callers cannot accidentally apply JSON parsing to a free-text prompt.
- All existing callers continue to work without behavior change unless they explicitly opt in.

**Non-goals**

- Replacing Strands / the agent graph framework. The fix lives at the prompt + `llm_service` boundary, not in `strands`.
- Changing model providers, model selection, or rate-limiting behavior.
- Migrating every existing caller to the new `generate_structured` API in this release. New API is added; migration is opportunistic and tracked separately.
- Streaming structured output. Out of scope; current callers consume full strings.
- Per-team prompt rewrites beyond `user_agent_founder` (other teams may benefit from Solutions 2/3 without prompt changes).

## Solution 1 — Align the Founder Spec to Markdown End-to-End

**Problem.** The lifecycle-graph node prompt asks for JSON; the agent prompt asks for Markdown; the consumer parses JSON. The Markdown output is the *correct* artifact — a product spec is a human document, not a structured payload.

**Change.**

- [`backend/agents/user_agent_founder/graphs/lifecycle_graph.py:30`](backend/agents/user_agent_founder/graphs/lifecycle_graph.py:30) — replace *"Return structured JSON with the specification."* with: *"Return the specification as a Markdown document. Do not wrap it in JSON or code fences."* The other three graph nodes (`submit_analysis`, `execute_build`, `review`) keep their JSON contract because their outputs are programmatic.
- [`backend/agents/user_agent_founder/agent.py:198`](backend/agents/user_agent_founder/agent.py:198) — `generate_spec()` returns `str` already. Document its return value in the docstring as *"raw Markdown — never parsed as JSON"*.
- The consumer that previously called `extract_json_from_response` on the spec output is changed to wrap the string in a code-side envelope: `{"spec_markdown": result}`. The envelope is built in Python, never asked of the LLM.

**Acceptance.**

- The "Startup Founder Testing Persona" run that produced the original `LLMJsonParseError` runs to completion against the same model and prompts.
- A unit test asserts `_call(SPEC_GENERATION_PROMPT)` returns a non-empty string and is **not** routed through `extract_json_from_response` anywhere in the lifecycle.

## Solution 2 — Structured-Output Guard with One Self-Correction Retry

**Problem.** When a JSON-shaped reply is genuinely required (e.g. `_parse_answer` at [`agent.py:136`](backend/agents/user_agent_founder/agent.py:136), or any caller of `complete_json`), a single mis-shaped response wastes the entire run. There is no automatic correction loop.

**Change.** Add a helper in `llm_service` that wraps `LLMClient.complete_json`:

```python
def complete_validated(
    client: LLMClient,
    prompt: str,
    *,
    schema: type[BaseModel],
    system_prompt: str | None = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
) -> BaseModel: ...
```

Behavior:

1. Issues the call with provider JSON mode enabled when available (the Ollama client already accepts a `format` arg in [`clients/ollama.py`](backend/agents/llm_service/clients/ollama.py)). Provider-mode forcing is opportunistic — falls back to plain prompt if the provider does not support it.
2. Parses the response via `extract_json_from_response`, then validates against `schema` (Pydantic).
3. On `LLMJsonParseError` **or** `pydantic.ValidationError`, performs one corrective follow-up call:

   > *"Your previous reply was not valid JSON matching the required schema. The error was: `{error}`. The required JSON schema is: `{schema.model_json_schema()}`. Re-emit ONLY the JSON object — no prose, no markdown, no code fences."*

   The original failed reply is included as prior context so the model can self-correct rather than regenerate from scratch.
4. If the corrective call also fails, raises the original `LLMJsonParseError` (unchanged blast radius for upstream code), but with a `correction_attempts_used` attribute populated so logs reveal that recovery was tried.

`correction_attempts` defaults to `1` — one auto-correction is the documented contract. Higher values are allowed but discouraged (cost / latency). `0` opts out (matches today's behavior).

**Telemetry.** Each corrected call logs a single `INFO` line: `"json_self_correction succeeded after 1 retry (schema=%s, model=%s)"`. Each fully-failed call logs `WARNING` with the schema, prompt hash, and 500-char preview — same payload `LLMJsonParseError` already carries.

**Acceptance.**

- Unit test in `backend/agents/llm_service/tests/test_structured_output.py` (new file): mocks an Ollama client that returns Markdown on call 1 and valid JSON on call 2, asserts `complete_validated` returns the parsed Pydantic model.
- Unit test for the failure-after-retry path: mock returns Markdown both times, asserts `LLMJsonParseError` raised with `correction_attempts_used == 1`.
- Unit test for the schema-validation path: mock returns syntactically-valid JSON missing a required field; asserts retry is attempted with the validation error embedded in the prompt.
- No existing test in `backend/agents/llm_service/tests/` regresses.

## Solution 3 — Split `generate_text` vs `generate_structured`

**Problem.** Today `LLMClient` exposes `complete`, `complete_text`, `complete_json`, and `chat_json_round` ([`interface.py:90`](backend/agents/llm_service/interface.py:90)). The naming does not telegraph the contract — `complete_text` internally calls `complete` which can fall through to `complete_json` parsing. The result: callers wire prompts asking for Markdown into methods that eventually hit `extract_json_from_response`. The founder bug is one symptom; the recurring `LLMJsonParseError` plans in `backend/agents/plans/` confirm it is a class.

**Change.** Add two thin, opinionated wrappers on top of the existing client:

```python
# backend/agents/llm_service/api.py  (new module)

def generate_text(
    prompt: str,
    *,
    system_prompt: str | None = None,
    temperature: float = 0.7,
    agent_key: str | None = None,
    think: bool = False,
) -> str:
    """Free-form text. Output is never JSON-parsed. Use for prose, markdown, code, etc."""

def generate_structured(
    prompt: str,
    *,
    schema: type[BaseModel],
    system_prompt: str | None = None,
    temperature: float = 0.0,
    agent_key: str | None = None,
    correction_attempts: int = 1,
) -> BaseModel:
    """Typed structured output. Internally enforces JSON mode + Solution 2 guard."""
```

Both delegate to the existing `get_client(agent_key)` plumbing; **no provider client changes**. The legacy methods (`complete`, `complete_text`, `complete_json`, `chat_json_round`) remain untouched and supported. The new module is purely additive.

A short note is added to [`backend/agents/llm_service/README.md`](backend/agents/llm_service/README.md) recommending the new entry points for new code.

**Lint guard (lightweight).** A `ruff` per-file rule or a small `tests/test_no_markdown_in_structured.py` static check scans `backend/agents/**/agent.py` for prompts whose body contains the word `markdown`/`prose`/`document` and verifies they are not passed to `complete_json` / `generate_structured`. Fails CI on new violations only (existing offenders allow-listed at introduction time, with a follow-up issue tracking each).

**Acceptance.**

- New module `backend/agents/llm_service/api.py` exports `generate_text` and `generate_structured`.
- `_parse_answer` at [`agent.py:136`](backend/agents/user_agent_founder/agent.py:136) is migrated to `generate_structured(...)` and its bespoke regex-stripping fallback is deleted (covered by Solution 2's guard).
- README updated with a one-paragraph "When to use which" section.
- CI lint check exists, currently green, with allow-list documented.

## Architecture & Module Touchpoints

| Area | File | Change kind |
|---|---|---|
| Founder graph prompt | [`backend/agents/user_agent_founder/graphs/lifecycle_graph.py`](backend/agents/user_agent_founder/graphs/lifecycle_graph.py) | Edit prompt string |
| Founder agent docstring | [`backend/agents/user_agent_founder/agent.py`](backend/agents/user_agent_founder/agent.py) | Docstring, migrate `_parse_answer` to `generate_structured` |
| Structured output guard | `backend/agents/llm_service/structured.py` | New |
| Public API wrappers | `backend/agents/llm_service/api.py` | New |
| Tests | `backend/agents/llm_service/tests/test_structured_output.py` | New |
| Static check | `backend/agents/llm_service/tests/test_no_markdown_in_structured.py` | New |
| Docs | [`backend/agents/llm_service/README.md`](backend/agents/llm_service/README.md) | Append section |
| Provider clients | [`backend/agents/llm_service/clients/ollama.py`](backend/agents/llm_service/clients/ollama.py) | **No change** — already exposes `format` arg |
| `LLMClient` interface | [`backend/agents/llm_service/interface.py`](backend/agents/llm_service/interface.py) | **No change** — additive only |

## Error Handling & Backwards Compatibility

- `LLMJsonParseError`'s public shape is preserved. A new optional attribute `correction_attempts_used: int = 0` is added; readers that don't know about it ignore it.
- All four existing `LLMClient` methods continue to behave as today. Solution 2 is opt-in via `complete_validated`; Solution 3 is opt-in via the new `api.py` module.
- The founder spec output envelope change (Solution 1) is the only behavioral change in an existing call path. It is covered by an updated test and a manual replay of the failing run.

## Telemetry & Observability

- `INFO`: one line per successful self-correction (`json_self_correction succeeded after N retries`).
- `WARNING`: one line per fully-failed validated call, including schema name and prompt hash (no full prompt to keep logs small).
- Existing `LLMJsonParseError` logs in `extract_json_from_response` and Ollama client are unchanged so dashboards keying on that error string continue to work.

## Rollout

1. Land Solution 1 alone — unblocks the immediate failing persona run with the smallest possible diff.
2. Land Solution 2 — recovers the broader class for all current `complete_json` callers via opt-in.
3. Land Solution 3 — additive API + lint guard. No mass migration required; `_parse_answer` is the canary migration.

Each step is independently revertable. Steps 2 and 3 ship with their own tests; step 1's acceptance is a successful replay of the originally failing run id.

## Risks

- **Self-correction loops the bill.** One extra LLM call per malformed reply. Capped at `correction_attempts=1` by default; surfaced in telemetry so cost is observable.
- **Schema injection in prompt.** Solution 2 inlines `schema.model_json_schema()` into the corrective prompt. For very large Pydantic models this could be lengthy. Mitigation: callers should keep schemas small; if needed, a future enhancement can summarize the schema rather than embed it verbatim.
- **Static lint false positives.** The "scan agent.py prompts for the word 'markdown'" check is heuristic. Allow-list is explicit, so false positives are a one-line annotation; new violations fail CI to catch the *intent* mismatch.
- **Strands integration.** The lifecycle graph's `build_agent` does not route through `complete_validated`. Solution 1 fixes the founder case directly via prompt alignment; broader Strands integration with the new guard is a follow-up tracked separately.
