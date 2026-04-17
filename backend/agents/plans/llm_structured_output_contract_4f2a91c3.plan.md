---
name: LLM structured-output contract — fix founder spec parse failure + harden llm_service
overview: "Implement the three-part fix described in backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md. (1) Align user_agent_founder spec generation to Markdown end-to-end so the failing 'Startup Founder Testing Persona' run no longer feeds Markdown into a JSON parser. (2) Add a complete_validated guard in llm_service that issues one schema-grounded self-correction retry on json/validation failures. (3) Add an additive generate_text / generate_structured public API plus a lightweight static check so future callers cannot accidentally route Markdown prompts into JSON parsing. Each phase is independently shippable and revertable."
todos:
  - id: s1-bypass-strands-for-spec
    content: In backend/agents/user_agent_founder/agent.py, add a private FounderAgent._call_text(prompt, *, system_prompt=None) helper that obtains the underlying LLMClient via llm_service.get_client(agent_key='user_agent_founder') and calls client.complete(prompt, system_prompt=system_prompt, temperature=0.7). Reuse the existing transient-error retry block from _call so flaky-network behavior is unchanged.
    status: pending
  - id: s1-generate-spec-route-text
    content: In backend/agents/user_agent_founder/agent.py, change FounderAgent.generate_spec (currently agent.py:198) to call self._call_text(SPEC_GENERATION_PROMPT, system_prompt=FOUNDER_SYSTEM_PROMPT) instead of self._call(SPEC_GENERATION_PROMPT). Update the docstring to state the return is raw Markdown and is never JSON-parsed downstream. Leave chat() and answer_question() on the existing Strands path.
    status: pending
  - id: s1-verify-no-json-consumer
    content: Confirm via grep that no caller of generate_spec parses its return as JSON. Specifically inspect orchestrator.run_workflow at backend/agents/user_agent_founder/orchestrator.py:356 — spec_content is stored verbatim and POSTed as a string body to /product-analysis/start-from-spec. Document the result of the audit in the PR description.
    status: pending
  - id: s1-no-edits-to-lifecycle-graph
    content: Explicitly do NOT edit backend/agents/user_agent_founder/graphs/lifecycle_graph.py. A repo-wide search shows build_lifecycle_graph has no caller — it is dead code and is not part of the failing flow. Earlier drafts of this plan misidentified it; record this guardrail in the PR description so reviewers can verify nothing in graphs/ was touched.
    status: pending
  - id: s1-replay-test
    content: Add a regression test in backend/agents/user_agent_founder/tests/test_agent_generate_spec.py that injects a stub LLMClient whose complete() returns the exact failing-run preview ('# TaskFlow MVP Product Specification\\n**Version:** 0.1 (MVP)...'). Assert generate_spec() returns that string unchanged AND assert the stub's chat_json_round was never invoked. The test must fail on main and pass after s1-generate-spec-route-text.
    status: pending
  - id: s2-structured-module
    content: Create backend/agents/llm_service/structured.py exposing complete_validated(client, prompt, *, schema, system_prompt=None, temperature=0.0, correction_attempts=1, **kwargs) -> BaseModel. Implementation calls client.complete_json (which already returns a parsed dict and already forces JSON mode internally via response_format), then schema.model_validate(data). On LLMJsonParseError or pydantic.ValidationError, performs up to correction_attempts corrective follow-up calls. The corrective prompt appends 'Error: {message}; Required schema: {schema.model_json_schema()}; Previous reply (truncated): {preview_or_data_repr}; Re-emit ONLY a JSON object — no prose, no markdown, no code fences.' For LLMJsonParseError use exc.response_preview; for ValidationError use json.dumps(data)[:500]. On final failure, re-raise the LAST error with correction_attempts_used set. Do NOT call extract_json_from_response — operate on the dict returned by complete_json.
    status: pending
  - id: s2-error-additions
    content: In backend/agents/llm_service/interface.py extend LLMJsonParseError.__init__ to accept and store correction_attempts_used: int = 0 (additive, default preserves today's signature). Also add a new LLMSchemaValidationError(LLMPermanentError) class with the same correction_attempts_used field so terminal pydantic.ValidationError failures can be re-raised with consistent shape.
    status: pending
  - id: s2-tests-success
    content: Add backend/agents/llm_service/tests/test_structured_output.py with a test that stubs LLMClient.complete_json to raise LLMJsonParseError(..., response_preview='# Markdown spec') on call 1 and return a valid dict on call 2; assert complete_validated returns the parsed Pydantic model and emits one INFO log line 'json_self_correction succeeded after 1 retry'.
    status: pending
  - id: s2-tests-failure
    content: In the same file, add a test where stubbed complete_json raises LLMJsonParseError both times; assert the error is re-raised with correction_attempts_used == 1 and a WARNING log line is emitted with the schema name and prompt hash.
    status: pending
  - id: s2-tests-validation
    content: In the same file, add a test where stubbed complete_json returns a dict missing a required schema field on call 1 and a complete dict on call 2; assert the corrective prompt embeds the Pydantic validation error string and that schema.model_validate succeeds on the second call's dict.
    status: pending
  - id: s2-tests-no-extract-call
    content: In the same file, add a test that asserts complete_validated never calls llm_service.util.extract_json_from_response — patch it with a sentinel that fails the test if invoked. This pins the contract that the helper layers on top of complete_json's parsed dict, not raw text.
    status: pending
  - id: s2-telemetry
    content: Wire INFO log on success and WARNING log on terminal failure exactly as defined in the spec's Telemetry section. Use the existing logger in llm_service; do not add a new logging dependency.
    status: pending
  - id: s3-api-module
    content: Create backend/agents/llm_service/api.py exporting generate_text(prompt, *, system_prompt=None, temperature=0.7, agent_key=None, think=False) -> str and generate_structured(prompt, *, schema, system_prompt=None, temperature=0.0, agent_key=None, correction_attempts=1) -> BaseModel. Both delegate to get_client(agent_key); generate_structured wraps complete_validated from s2-structured-module. No provider client changes.
    status: pending
  - id: s3-readme
    content: Append a 'When to use which' subsection to backend/agents/llm_service/README.md explaining generate_text vs generate_structured, with a one-sentence pointer to FEATURE_SPEC_structured_output_contract.md and a note that the legacy complete / complete_text / complete_json methods remain supported.
    status: pending
  - id: s3-canary-migration
    content: Migrate FounderAgent._parse_answer (backend/agents/user_agent_founder/agent.py:136) to use generate_structured with a Pydantic model FounderAnswer(selected_option_id: str, other_text: str | None, rationale: str). Delete the bespoke regex stripping and the AttributeError fallback path; the new guard covers them.
    status: pending
  - id: s3-lint-check
    content: Add backend/agents/llm_service/tests/test_no_markdown_in_structured.py that walks backend/agents/**/*.py, finds string literals assigned to *_PROMPT names containing 'markdown' / 'prose' / 'document', and asserts they are not passed to complete_json or generate_structured at any call site. Allow-list existing offenders explicitly; new violations fail CI.
    status: pending
  - id: s3-allowlist-doc
    content: In the new test_no_markdown_in_structured.py, document each allow-listed offender with a one-line comment naming the prompt, the file:line, and a follow-up issue/plan reference. The list should be trivially auditable.
    status: pending
  - id: verify-existing-tests
    content: Run pytest under backend/agents/llm_service/tests/ and backend/agents/user_agent_founder/tests/ to confirm no regression. The full per-team test suites in CI (SE, blogging, market research, etc.) must remain green; if any structured-output caller relied on the old _parse_answer fallback shape, fix at the call site rather than in the new helper.
    status: pending
  - id: lint-format
    content: Run cd backend && make lint-fix, then make lint to confirm ruff check + format are clean against the new files. Line length 120, Python 3.10 target per pyproject.toml.
    status: pending
  - id: changelog
    content: Add a CHANGELOG.md entry summarizing the three-part fix and pointing to backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md. Mention the new public API (generate_text / generate_structured) and that legacy methods are unchanged.
    status: pending
isProject: false
---

# Plan: Implement LLM structured-output contract

This plan implements the design in [backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md](backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md). The originating failure is the "Startup Founder Testing Persona" run that surfaced `LLMJsonParseError: Could not parse structured JSON from LLM response. Response preview: '# TaskFlow MVP Product Specification...'`.

The work is sequenced into three phases. **Each phase is independently mergeable, individually testable, and revertable in isolation.** Phase 1 unblocks the immediate failing run. Phase 2 reduces the error class for all current `complete_json` callers via opt-in. Phase 3 makes the right pattern obvious for new code.

---

## Phase 1 — Route founder spec generation through a text-only LLM call

**Why this is first.** Smallest diff that makes the failing persona run succeed. The actual failure path is: `orchestrator.run_workflow → FounderAgent.generate_spec → self._agent(prompt)` (Strands) → `LLMClientModel.stream` → `LLMClient.chat_json_round` (JSON-only). A Markdown prompt cannot survive `chat_json_round`. The fix bypasses Strands for *this one call*; everything else stays.

**Important corrections from earlier draft**

- `build_lifecycle_graph` in `graphs/lifecycle_graph.py` is dead code. **Do not edit it.** The earlier draft of this plan targeted that file; it was the wrong target and would have changed nothing observable.
- `orchestrator.run_workflow` at [orchestrator.py:356](backend/agents/user_agent_founder/orchestrator.py:356) already treats `spec_content` as an opaque string. No envelope wrapping is needed.

**Files touched**

- [backend/agents/user_agent_founder/agent.py](backend/agents/user_agent_founder/agent.py) — add `_call_text` helper, route `generate_spec` through it, update docstring.
- `backend/agents/user_agent_founder/tests/test_agent_generate_spec.py` — new regression test asserting the Markdown preview is returned and `chat_json_round` is never invoked.

**Acceptance**

- The failing run id replays end-to-end on the same model + same prompts.
- `pytest backend/agents/user_agent_founder/tests/` is green.
- `git diff` shows no edits under `backend/agents/user_agent_founder/graphs/`.

**Risks**

- `client.complete()` for some providers may delegate to `complete_json` (default fallback in [interface.py:121](backend/agents/llm_service/interface.py:121)). The Ollama client overrides `complete` properly ([clients/ollama.py:1116](backend/agents/llm_service/clients/ollama.py:1116)), but the `Dummy` client and any future provider must be checked. Mitigation: the regression test injects a stub `LLMClient` whose `complete` returns Markdown — if a provider routes through `complete_json` the test will catch it.

---

## Phase 2 — `complete_validated` guard with one self-correction retry

**Why this is second.** Once Phase 1 lands, the immediate fire is out. Phase 2 generalizes: any other team that genuinely needs JSON gets one schema-grounded re-ask before failure.

**Design notes for the implementer**

- The guard lives in a new file `backend/agents/llm_service/structured.py` rather than patched into `interface.py`, because the interface should remain a thin abstract contract over providers.
- The helper consumes the **already-parsed dict** returned by `LLMClient.complete_json` ([interface.py:99](backend/agents/llm_service/interface.py:99)) and runs `schema.model_validate(data)` on it. Do NOT call `extract_json_from_response` from the helper — provider clients handle parsing internally and surface failures via `LLMJsonParseError`. There is a dedicated test (`s2-tests-no-extract-call`) pinning this contract.
- Provider JSON mode is **already on, unconditionally**, inside `complete_json` (Ollama sets `payload["response_format"] = {"type":"json_object"}` at [clients/ollama.py:977](backend/agents/llm_service/clients/ollama.py:977)). The helper does not need to enable it. There is no `format=` kwarg to pass.
- The corrective-prompt template is defined once in `structured.py` and includes: the validation/parse error string, the schema JSON, and the prior failed reply (from `exc.response_preview` for parse errors, `json.dumps(data)[:500]` for validation errors). Keep schemas small to avoid prompt bloat.
- `LLMJsonParseError.correction_attempts_used` and the new `LLMSchemaValidationError.correction_attempts_used` are informational. No upstream caller needs to read them; logs and dashboards can.

**Files touched**

- New: `backend/agents/llm_service/structured.py`
- Edit (additive only): [backend/agents/llm_service/interface.py](backend/agents/llm_service/interface.py) — extend `LLMJsonParseError.__init__` with `correction_attempts_used`; add `LLMSchemaValidationError(LLMPermanentError)`.
- New: `backend/agents/llm_service/tests/test_structured_output.py` (four tests: success after retry, terminal failure, validation re-ask, no-extract-call contract).

**Acceptance**

- New tests pass. Existing `backend/agents/llm_service/tests/` suite is green.
- A unit test asserts the corrective prompt actually embeds the validation error and schema (not just retries blindly).
- `make lint` clean.

**Risks**

- Extra LLM call per malformed reply ⇒ cost. Capped at `correction_attempts=1` default; surfaced via INFO logs so cost is observable.
- Large Pydantic schemas blow up corrective prompt size. Mitigation: documented; canary migration uses a 3-field model.

---

## Phase 3 — `generate_text` / `generate_structured` API + lint guard

**Why this is third.** Phase 2 is reactive — it recovers from the bug. Phase 3 is preventive — it makes the bug structurally hard to introduce. The new API is purely additive; legacy methods stay.

**Design notes for the implementer**

- The new module `backend/agents/llm_service/api.py` is a thin facade. `generate_text` delegates to the existing `complete` path. `generate_structured` delegates to `complete_validated` from Phase 2.
- The canary migration of `_parse_answer` proves the new API on a real call site and lets us delete a chunk of bespoke fallback logic.
- The lint check is a runtime test, not a ruff rule, because the heuristic ("a `*_PROMPT` constant whose body says 'markdown' is being passed to a JSON-expecting method") needs AST-level inspection. Keep it scoped to `backend/agents/**/*.py` to avoid noise.
- Allow-list existing offenders explicitly with file:line and a follow-up reference. The list should not exceed a handful; if it does, that is signal that more migrations belong in this plan.

**Files touched**

- New: `backend/agents/llm_service/api.py`
- New: `backend/agents/llm_service/tests/test_no_markdown_in_structured.py`
- Edit: [backend/agents/llm_service/README.md](backend/agents/llm_service/README.md) — appendsubsection.
- Edit: [backend/agents/user_agent_founder/agent.py](backend/agents/user_agent_founder/agent.py) — migrate `_parse_answer` to `generate_structured`; delete dead fallback.

**Acceptance**

- `_parse_answer` migration tested via the existing answer-question call sites; behavior unchanged for valid inputs.
- The static check is green with the documented allow-list.
- `make lint` clean. Per-team test suites green in CI.

**Risks**

- False positives in the static check. Mitigation: explicit allow-list, scoped path, runs as a unit test (not a ruff rule), so silencing a false positive is one allow-list line.
- Strands integration: `build_agent` in `lifecycle_graph.py` does not yet flow through `complete_validated`. Phase 1's prompt fix covers the founder case; broader Strands wiring is a deliberate follow-up, not in scope here.

---

## Verification matrix

| Check | Where | Phase |
|---|---|---|
| Failing-run replay test | `backend/agents/user_agent_founder/tests/` | 1 |
| Self-correction success path | `backend/agents/llm_service/tests/test_structured_output.py` | 2 |
| Self-correction failure path (raises with attribute) | same | 2 |
| Schema validation re-ask path | same | 2 |
| `_parse_answer` canary migration | existing user_agent_founder tests | 3 |
| Markdown-in-structured static check | `backend/agents/llm_service/tests/test_no_markdown_in_structured.py` | 3 |
| `make lint` (ruff check + format) | repo-wide | every phase |
| Per-team CI suites green | GitHub Actions | every phase |

## Rollback

Each phase reverts cleanly:

- **Phase 1**: revert two file edits + delete the regression test. The failing run reappears (acceptable since older behavior is restored).
- **Phase 2**: delete `structured.py` and the new test file; remove the additive kwarg on `LLMJsonParseError.__init__`. No legacy caller depended on either.
- **Phase 3**: delete `api.py`, the static check, and the README subsection; revert the `_parse_answer` migration. Legacy `complete_json` path is fully intact.

## Out of scope (explicitly)

Tracked separately, not in this plan:

- Renaming the `user_agent_founder` module / route.
- Streaming structured output.
- Wiring the `complete_validated` guard into Strands `build_agent` directly.
- Bulk migration of every existing `complete_json` caller to `generate_structured`.
