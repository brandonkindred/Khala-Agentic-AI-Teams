# Code review: think-then-format JSON split

**Date:** 2026-08-12  
**Status:** Approved for implementation planning  
**Scope:** `software_engineering_team/code_review_agent` only

## Problem

Code-review paths that need structured JSON today call `complete_validated` /
`complete_json`, or a Strands `Agent` backed by a model whose default
`response_format` is `"json"`.

In `llm_service`, `response_format="json"` forces thinking off when the caller
does not explicitly override `think` (see `resolve_think_for_model`). That
disables the model’s highest thinking tier on the actual review work — the
call that most needs it — because JSON decoding and extended thinking compete
for the content channel.

## Goal

Every code-review LLM path whose *outcome* is structured JSON must become two
requests:

1. **Review / reasoning** — free-form (or tool-using) analysis with thinking
   set to the highest level available for the active model (`think=True`,
   which upgrades to the model’s max registered level). **No** JSON
   `response_format` on this call.
2. **Formatting** — a second request that transcribes the review prose into
   the target JSON shape with `think=False` and JSON response format (via
   `complete_json` / `complete_validated`). **No tools** on this call.

The review prompt must not ask for JSON output. JSON schema / output-contract
instructions move to the formatting call only.

## Non-goals

- Do **not** modify `llm_service.complete_json_via_reasoning` /
  `complete_validated_via_reasoning` (callers outside code review keep today’s
  API; those helpers still hard-reject `think=` overrides).
- Do **not** change non–code-review agents in this work.
- Do **not** remove the coordinator’s last-resort thinking-off retry; that
  retry keeps the two-call split, with call 1 using `think=False`.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Scope | All code-review JSON outcome paths (chunk review, FPF, submission passes, synthesis, and any sibling Agent/`complete_validated` JSON path under `code_review_agent/`) |
| Thinking-off retry | Keep split; prose/review call uses `think=False`; format call still `think=False` + JSON |
| Where the split lives | Code-review-local wrapper only (`code_review_agent/via_reasoning.py`); leave `llm_service` helpers unchanged |
| Tool-using passes | Same pattern: tools + max think + text on call 1; no tools + JSON + think off on call 2 |
| Prompt strategy | Split prompts: reasoning = criteria + “structured prose”; formatting = existing JSON contract + wrapped prose |

## Architecture

### New module: `code_review_agent/via_reasoning.py`

Local twin of the `llm_service` via-reasoning helpers, with two entry points
that share delimiter / untrusted-analysis guard behavior (copied or thin
re-use of private helpers only if already exported; otherwise duplicate the
small delimiter wrap — do not import private `llm_service.structured`
internals).

#### 1. `complete_validated_via_reasoning_local`

For `LLMClient` call sites (primarily `ChunkReviewAgent`).

```
prose = client.complete(
    reasoning_prompt,
    system_prompt=reasoning_system_prompt,  # no JSON contract
    think=reasoning_think,                  # True by default → model max; False for last-resort retry
    response_format implicitly text via complete(),
    ...
)
return complete_validated(
    client,
    format_prompt,                          # formatting_instructions + wrapped prose
    schema=...,
    think=False,
    system_prompt=formatting_system_prompt + untrusted-analysis guard,
    ...
)
```

**Contract:**
- Preconditions: non-empty `objective`, `reasoning_prompt`, and either
  explicit `formatting_instructions` or a default derived from the schema
  field list; `reasoning_system_prompt` must not end with JSON-only
  instructions (caller obligation, enforced by prompt split below).
- `reasoning_think`: `None`/`True` → highest thinking for the model; explicit
  `False` (and any string level) forwarded to call 1 only. Call 2 always
  `think=False`.
- Postconditions: returns a validated Pydantic instance (same as today’s
  `complete_validated`). Call-1 failure skips call 2 and propagates.

#### 2. `run_agent_via_reasoning`

For Strands `Agent` call sites (false-positive filter, submission-pass runner,
synthesis, and any other Agent-based JSON path).

```
# Call 1 — review
text_model = resolve/clone with response_format="text", think=reasoning_think
agent = Agent(model=text_model, system_prompt=reasoning_system_prompt, tools=tools or [])
prose = str(agent(reasoning_prompt)).strip()

# Call 2 — format (no tools)
# Prefer LLMClient.complete_json / complete_validated when an underlying
# LLMClient is available; otherwise a no-tools Agent on a JSON-mode model
# with think=False.
parsed = format_to_json(prose, formatting_instructions, schema_or_parse)
```

**Contract:**
- Tools are attached only to call 1.
- Call 2 never receives tools.
- Callers supply already-split prompts; the wrapper does not rewrite prompt
  text beyond wrapping prose with analysis delimiters and appending the
  untrusted-analysis guard to the formatting system prompt.

### Prompt split (all profiles / passes)

| Piece | Reasoning call | Formatting call |
|---|---|---|
| Role, criteria, guardrails, code/context | Yes | No (prose already contains the findings) |
| “Answer in structured prose” instruction | Yes | No |
| `_SHARED_OUTPUT_SECTION` / pass-specific JSON output blocks / `JSON_OUTPUT_INSTRUCTION` | **No** | Yes |
| `FINAL_OUTPUT_CONTRACT_NOTE` (chunk user prompt) | **Removed** from reasoning | Superseded by formatting instructions |
| Approval / severity semantics that affect *what* to find | Stay on reasoning (as review policy) | Re-stated on formatting only as field/shape constraints needed to transcribe faithfully |

Concrete refactor for chunk review:
- `build_review_system_prompt` (or a sibling composer) exposes
  `build_review_reasoning_system_prompt` and
  `build_review_formatting_instructions` (or equivalent), so the byte-stable
  “full prompt” used by tests is either updated deliberately or composed as
  `reasoning + formatting` with tests adjusted to assert the split parts.
- User prompt builder stops appending `FINAL_OUTPUT_CONTRACT_NOTE` on the
  reasoning call.

Other passes (`prompts.py` FPF / architecture / side-effect / synthesis):
same split — body without JSON tail for reasoning; JSON contract moved to
formatting instructions.

### Call-site migration

| Call site | Today | After |
|---|---|---|
| `chunk_reviewer._run_chunk_review` | `complete_validated(...)` | `complete_validated_via_reasoning_local(...)`; forwards `think` to `reasoning_think` |
| `submission_pass_runner._call_agent` | one `Agent` + `parse(raw)` | `run_agent_via_reasoning` (tools on call 1); `parse` runs on formatting JSON (or validated schema when one exists) |
| `false_positive_filter._verify_group` | one tool `Agent` + `extract_json_from_response` | same via `run_agent_via_reasoning` |
| `synthesis.synthesize_*` | one no-tool `Agent` + JSON parse | `run_agent_via_reasoning` with `tools=[]` |
| Merged / architecture / side-effect passes that go through `run_submission_pass` | inherit via `_call_agent` | no per-pass duplicate once runner is updated |

### Error handling and recovery

- A failure on call 1 (network, semantic exhaustion, truncation, tool errors)
  propagates; call 2 is not invoked. Existing coordinator recovery
  (retry, bisection, thinking-off retry) treats the split as **one logical
  review attempt**.
- A failure on call 2 only (JSON parse / schema validation) is a recoverable
  content failure, same class as today’s malformed JSON from a single-shot
  call. Prefer letting `complete_validated`’s corrective retry apply on the
  **formatting** call before bubbling up. Do not re-run the expensive review
  call solely to fix formatting unless the coordinator’s existing retry
  policy already re-runs the whole logical unit.
- Thinking-off retry (`mapping.py` → `think=False`): still invokes the
  wrapper; call 1 uses `think=False` + text; call 2 remains JSON +
  `think=False`.

### Cost / latency

Expect roughly **2× LLM requests** (and higher wall time) on every migrated
path. That is an accepted trade-off for restoring max thinking on review
work. No env flag is required for v1; a kill-switch may be added later if
ops needs one, but it is out of scope for the initial plan unless
implementation discovers a hard need.

## Testing

- Unit tests for `via_reasoning.py`: sequence (complete/Agent then
  complete_json), `reasoning_think` forwarding, tools absent on call 2,
  call-1 failure skips call 2, delimiter wrap present in format prompt.
- Chunk reviewer tests: assert no JSON response format / no
  `FINAL_OUTPUT_CONTRACT_NOTE` on call 1; schema validation still on call 2;
  `think=False` path still splits.
- Submission-pass / FPF tests: tool calls only on first Agent; second call
  has empty tools; existing parse/verdict behavior preserved.
- Synthesis tests: two calls; narrative fields still parsed.
- Update profile/prompt tests that currently assert a single composed prompt
  contains both criteria and JSON contract — they must assert the split
  surfaces instead.
- Do not weaken coverage gates; new module targets ≥90% line coverage.

## Rollout

1. Land `via_reasoning.py` + tests.
2. Migrate chunk reviewer (highest-volume, clearest `LLMClient` path).
3. Migrate `submission_pass_runner` (covers architecture / side-effect passes).
4. Migrate false-positive filter and synthesis.
5. Grep `code_review_agent` for remaining `complete_validated` /
   `complete_json` / single-shot `Agent(` + JSON parse paths; migrate or
   document any intentional exception (there should be none under this
   spec).

## Success criteria

- No code-review path that returns structured findings/verdicts issues a
  review/reasoning request with `response_format="json"`.
- Default review/reasoning calls use max thinking for the active model
  (`think=True` resolution path).
- Formatting calls always use `think=False` and JSON mode, with no tools.
- Coordinator thinking-off retry still works and still uses the two-call
  split.
- Existing public review outputs (`ChunkReviewOutput`, issue lists, FPF
  keep/drop behavior, synthesis digests) remain shape-compatible.
