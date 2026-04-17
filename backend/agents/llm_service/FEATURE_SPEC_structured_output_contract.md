# Feature Spec: Structured-Output Contract for LLM Calls

## Context

A `user_agent_founder` "Startup Founder Testing Persona" run failed with:

> `LLMJsonParseError: Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. Response preview: '# TaskFlow MVP Product Specification\n**Version:** 0.1 (MVP)...'`

The model did exactly what it was asked to do. The bug is a **prompt-asks-for-Markdown but transport-forces-JSON** mismatch on the executed call path:

- The live flow is `api/main.py → orchestrator.run_workflow → FounderAgent.generate_spec` ([`orchestrator.py:356`](backend/agents/user_agent_founder/orchestrator.py:356)), which calls `self._agent(prompt)` ([`agent.py:179`](backend/agents/user_agent_founder/agent.py:179)) — a Strands `Agent` wrapping our `LLMClientModel`.
- Strands `Model.stream` routes every turn through [`LLMClient.chat_json_round`](backend/agents/llm_service/strands_adapter.py:225) (around line 270), which is a JSON-only path: the underlying Ollama client sends `response_format={"type":"json_object"}` ([`clients/ollama.py:977`](backend/agents/llm_service/clients/ollama.py:977), [`clients/ollama.py:1271`](backend/agents/llm_service/clients/ollama.py:1271)) and parses the body internally; on parse failure it raises `LLMJsonParseError` ([`clients/ollama.py:490`](backend/agents/llm_service/clients/ollama.py:490)).
- The prompt being sent is [`SPEC_GENERATION_PROMPT`](backend/agents/user_agent_founder/agent.py:70), which explicitly asks for *"a markdown document with these sections..."*. The model obliges; the JSON-only transport then rejects it.

> **Note on `lifecycle_graph.py`.** A `build_lifecycle_graph` exists in [`graphs/lifecycle_graph.py`](backend/agents/user_agent_founder/graphs/lifecycle_graph.py) but a repo-wide search shows no caller — it is currently dead code. Its `generate_spec` node prompt ("Return structured JSON…") is **not** what triggers the failure. Earlier drafts of this spec misidentified it as the contract source; that has been corrected here.

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

## Solution 1 — Route Founder Spec Generation Through a Text-Only LLM Call

**Problem.** `FounderAgent.generate_spec` builds a Markdown-asking prompt and pushes it through a Strands `Agent`. Strands `LLMClientModel.stream` always routes to `chat_json_round`, which forces `response_format=json_object` and JSON-parses the response. A Markdown reply cannot survive that path. The fix is to **stop running free-form text prompts through the JSON-shaped Strands transport** for the spec call specifically — not to edit the dead `lifecycle_graph.py` node.

**Change.**

- In [`backend/agents/user_agent_founder/agent.py`](backend/agents/user_agent_founder/agent.py), replace the spec-generation path so it bypasses Strands. Concretely:
  - Add a private helper `_call_text(prompt: str, *, system_prompt: str | None = None) -> str` that obtains the underlying `LLMClient` via `llm_service.get_client(agent_key="user_agent_founder")` and calls `client.complete(prompt, system_prompt=system_prompt, temperature=0.7)`. Reuse the existing transient-error retry block from [`_call`](backend/agents/user_agent_founder/agent.py:169) so behavior is unchanged on flaky networks.
  - `generate_spec()` ([`agent.py:198`](backend/agents/user_agent_founder/agent.py:198)) calls `self._call_text(SPEC_GENERATION_PROMPT, system_prompt=FOUNDER_SYSTEM_PROMPT)` instead of `self._call(SPEC_GENERATION_PROMPT)`. Update the docstring to state the return is raw Markdown and is never JSON-parsed downstream.
  - Leave `chat()` and `answer_question()` on the existing Strands path — `answer_question` is the one call that legitimately wants JSON and is the canary migration in Solution 3.
- Drop the dead `build_lifecycle_graph` references from this spec entirely (done in Context above). If the dead code remains a foot-gun risk, removing the `graphs/lifecycle_graph.py` file is a small, separate cleanup PR — explicitly out of scope here.
- Verify `orchestrator.run_workflow` ([`orchestrator.py:356`](backend/agents/user_agent_founder/orchestrator.py:356)) treats `spec_content` as an opaque string (it does — it stores it via `store.update_run(spec_content=…)` and POSTs it as a string body to `/product-analysis/start-from-spec`). No orchestrator change required.

**Acceptance.**

- The "Startup Founder Testing Persona" run that produced the original `LLMJsonParseError` runs to completion against the same model and prompts.
- A unit test injects a stubbed `LLMClient` whose `complete` returns the exact failing-run preview (`# TaskFlow MVP Product Specification\n**Version:** 0.1 (MVP)…`); asserts `generate_spec()` returns it unchanged and that `chat_json_round` was **not** invoked.
- A grep verifies no caller of `generate_spec` parses its return value as JSON.

## Solution 2 — Structured-Output Guard with One Self-Correction Retry

**Problem.** When a JSON-shaped reply is genuinely required (e.g. `_parse_answer` at [`agent.py:136`](backend/agents/user_agent_founder/agent.py:136), or any caller of `complete_json` / `chat_json_round`), a single mis-shaped response wastes the entire run. There is no automatic correction loop and no Pydantic validation at the boundary.

**Current contract (do not break).**

- `LLMClient.complete_json` returns `dict[str, Any]` — **already parsed**. Internal JSON parsing happens inside the provider client (e.g. `OllamaLLMClient._extract_json` raising `LLMJsonParseError` at [`clients/ollama.py:490`](backend/agents/llm_service/clients/ollama.py:490)).
- The Ollama client forces JSON mode by setting `payload["response_format"] = {"type": "json_object"}` ([`clients/ollama.py:977`](backend/agents/llm_service/clients/ollama.py:977), [`clients/ollama.py:1271`](backend/agents/llm_service/clients/ollama.py:1271)). There is no `format=` kwarg on the public method — JSON mode is implicit and unconditional for `complete_json` / `chat_json_round`.
- The `last failed reply` text is **not** returned from `complete_json` on failure; it is only available via the 500-char `LLMJsonParseError.response_preview` field.

**Change.** Add a thin helper in `llm_service` that layers Pydantic validation + a corrective re-call **on top of** `complete_json` — no changes to provider clients, no calls to `extract_json_from_response` from the helper:

```python
def complete_validated(
    client: LLMClient,
    prompt: str,
    *,
    schema: type[BaseModel],
    system_prompt: str | None = None,
    temperature: float = 0.0,
    correction_attempts: int = 1,
    **kwargs: Any,
) -> BaseModel: ...
```

Behavior:

1. Calls `client.complete_json(prompt, system_prompt=system_prompt, temperature=temperature, **kwargs)` and receives a `dict`. JSON mode is already on inside the provider — nothing to configure.
2. Validates the dict via `schema.model_validate(data)`.
3. On `LLMJsonParseError` **or** `pydantic.ValidationError`, performs up to `correction_attempts` corrective follow-up calls. Each correction call is a fresh `complete_json` call whose user prompt is the original prompt **plus** an appended block:

   > *"Your previous reply was rejected. Error: `{error_message}`. Required JSON schema: `{schema.model_json_schema()}`. Re-emit ONLY a JSON object satisfying this schema — no prose, no markdown, no code fences. The previous reply (truncated) was: `{response_preview_or_repr_data}`."*

   For an `LLMJsonParseError`, the helper uses `exc.response_preview`. For a `ValidationError`, it uses `json.dumps(data)[:500]`. Either way the model gets enough signal to self-correct rather than regenerate from scratch.
4. If every corrective call also fails, re-raises the **last** error (`LLMJsonParseError` or `ValidationError`) with a `correction_attempts_used` attribute populated (added to `LLMJsonParseError`; for `ValidationError` we wrap into a new `LLMSchemaValidationError(LLMPermanentError)` declared in `interface.py`).

`correction_attempts` defaults to `1` — one auto-correction is the documented contract. Higher values are allowed but discouraged (cost / latency). `0` opts out (matches today's behavior).

**Telemetry.** Each corrected call logs a single `INFO` line: `"json_self_correction succeeded after 1 retry (schema=%s, model=%s)"`. Each fully-failed call logs `WARNING` with the schema, prompt hash, and 500-char preview — same payload `LLMJsonParseError` already carries.

**Acceptance.**

- Unit test in `backend/agents/llm_service/tests/test_structured_output.py` (new file): stubs `LLMClient.complete_json` to raise `LLMJsonParseError(..., response_preview="# Markdown")` on call 1 and return a valid dict on call 2; asserts `complete_validated` returns the parsed Pydantic model.
- Unit test for the failure-after-retry path: stubbed `complete_json` raises `LLMJsonParseError` both times; asserts the error is re-raised with `correction_attempts_used == 1`.
- Unit test for the schema-validation path: stubbed `complete_json` returns a dict missing a required schema field on call 1 and a complete dict on call 2; asserts the corrective prompt embeds the Pydantic validation error string and the second call's parsed model is returned.
- Unit test confirming `complete_validated` does **not** call `extract_json_from_response` (it operates on the dict returned by `complete_json`).
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
| Provider clients | [`backend/agents/llm_service/clients/ollama.py`](backend/agents/llm_service/clients/ollama.py) | **No change** — JSON mode already on internally via `response_format` |
| `LLMClient` interface | [`backend/agents/llm_service/interface.py`](backend/agents/llm_service/interface.py) | Additive: extend `LLMJsonParseError.__init__` with `correction_attempts_used`; add `LLMSchemaValidationError(LLMPermanentError)` |

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
- **Strands integration.** Strands' `LLMClientModel.stream` always routes through `chat_json_round`, so any free-form text prompt sent through a Strands `Agent` will hit the JSON path. Solution 1 sidesteps this for the founder spec by using `client.complete()` directly. Broader Strands integration — e.g. a "free-text" Strands `Model` variant that flows through `complete()` — is a deliberate follow-up, not in scope here.

- **Dead lifecycle graph.** [`graphs/lifecycle_graph.py`](backend/agents/user_agent_founder/graphs/lifecycle_graph.py) is currently uncalled. This spec does **not** edit it (earlier drafts did, in error). Removing the file is a separate cleanup PR; until then, future readers should not assume the founder runs through it.
