---
name: LLM structured-output contract — fix founder spec parse failure + harden llm_service
overview: "Implement the three-part fix described in backend/agents/llm_service/FEATURE_SPEC_structured_output_contract.md. (1) Align user_agent_founder spec generation to Markdown end-to-end so the failing 'Startup Founder Testing Persona' run no longer feeds Markdown into a JSON parser. (2) Add a complete_validated guard in llm_service that issues one schema-grounded self-correction retry on json/validation failures. (3) Add an additive generate_text / generate_structured public API plus a lightweight static check so future callers cannot accidentally route Markdown prompts into JSON parsing. Each phase is independently shippable and revertable."
todos:
  - id: s1-graph-prompt-markdown
    content: In backend/agents/user_agent_founder/graphs/lifecycle_graph.py, replace the generate_spec node system prompt 'Return structured JSON with the specification.' with 'Return the specification as a Markdown document. Do not wrap it in JSON or code fences.' Leave submit_analysis, execute_build, review nodes' JSON contracts untouched.
    status: pending
  - id: s1-agent-docstring
    content: In backend/agents/user_agent_founder/agent.py, update FounderAgent.generate_spec docstring to state the return value is raw Markdown and is never parsed as JSON downstream.
    status: pending
  - id: s1-consumer-envelope
    content: Find the consumer that currently passes generate_spec output through extract_json_from_response (grep extract_json_from_response and complete_json under backend/agents/user_agent_founder/ and any orchestrator that handles its output). Wrap the spec string in a code-built envelope {'spec_markdown': result} instead of asking the LLM for JSON. If no such consumer exists today, document the contract in code comments at the call site so future maintainers do not re-introduce the parse.
    status: pending
  - id: s1-replay-test
    content: Add a regression test in backend/agents/user_agent_founder/tests/ that calls generate_spec with a stubbed LLM client returning the exact failing-run preview ('# TaskFlow MVP Product Specification\\n**Version:** 0.1 (MVP)...') and asserts the lifecycle completes without LLMJsonParseError. The test must fail on main before s1-graph-prompt-markdown lands and pass after.
    status: pending
  - id: s2-structured-module
    content: Create backend/agents/llm_service/structured.py exposing complete_validated(client, prompt, *, schema, system_prompt=None, temperature=0.0, correction_attempts=1) -> BaseModel. Implementation calls client.complete_json with provider JSON mode when supported (Ollama format='json'), parses via extract_json_from_response, validates against schema (Pydantic). On LLMJsonParseError or pydantic.ValidationError, performs one corrective follow-up call embedding the previous reply and the schema.model_json_schema(); on second failure, re-raises the original LLMJsonParseError with a new attribute correction_attempts_used set.
    status: pending
  - id: s2-error-attribute
    content: In backend/agents/llm_service/interface.py, extend LLMJsonParseError.__init__ to accept and store correction_attempts_used: int = 0 (additive, default preserves today's signature). No call sites need updates.
    status: pending
  - id: s2-tests-success
    content: Add backend/agents/llm_service/tests/test_structured_output.py with a test that mocks an Ollama client returning Markdown on call 1 and valid JSON on call 2; assert complete_validated returns the parsed Pydantic model and emits one INFO log line 'json_self_correction succeeded after 1 retry'.
    status: pending
  - id: s2-tests-failure
    content: In the same file, add a test where the mock returns Markdown twice; assert LLMJsonParseError is raised with correction_attempts_used == 1 and a WARNING log line is emitted with the schema name and prompt hash.
    status: pending
  - id: s2-tests-validation
    content: In the same file, add a test where the mock returns syntactically-valid JSON missing a required schema field on call 1 and a complete object on call 2; assert the corrective prompt embeds the validation error string and the second call's parsed model is returned.
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

## Phase 1 — Align founder spec to Markdown end-to-end

**Why this is first.** Smallest possible diff that makes the failing persona run succeed. Pure prompt + plumbing change; no new abstractions.

**Files touched**

- [backend/agents/user_agent_founder/graphs/lifecycle_graph.py](backend/agents/user_agent_founder/graphs/lifecycle_graph.py) — line 30 prompt edit only.
- [backend/agents/user_agent_founder/agent.py](backend/agents/user_agent_founder/agent.py) — `generate_spec` docstring + (if a parse-the-spec consumer exists) replace it with a Python-built envelope.
- `backend/agents/user_agent_founder/tests/` — add the regression replay test.

**Acceptance**

- The failing run id replays end-to-end on the same model + same prompts.
- `pytest backend/agents/user_agent_founder/tests/` is green.
- A grep for `extract_json_from_response` under `backend/agents/user_agent_founder/` shows no path that consumes the spec output as JSON.

**Risks**

- A downstream consumer may currently rely on the spec being JSON-shaped. Mitigation: the consumer audit in `s1-consumer-envelope` is explicit; the envelope is built in code so the consumer's interface (a dict with `spec_markdown`) is unchanged in shape.

---

## Phase 2 — `complete_validated` guard with one self-correction retry

**Why this is second.** Once Phase 1 lands, the immediate fire is out. Phase 2 generalizes: any other team that genuinely needs JSON gets one schema-grounded re-ask before failure.

**Design notes for the implementer**

- The guard lives in a new file `backend/agents/llm_service/structured.py` rather than patched into `interface.py`, because the interface should remain a thin abstract contract over providers.
- Provider JSON mode is opportunistic: pass `format='json'` to Ollama via existing `clients/ollama.py` plumbing. If a future provider does not support it, fall through to plain prompt — the corrective retry still works.
- The corrective prompt template is defined once in `structured.py` and includes: the validation/parse error string, the schema JSON, and the prior failed reply. Keep schemas small to avoid prompt bloat (called out in the spec's Risks).
- `LLMJsonParseError.correction_attempts_used` is purely informational. No upstream caller needs to read it; logs and dashboards can.

**Files touched**

- New: `backend/agents/llm_service/structured.py`
- Edit (additive only): [backend/agents/llm_service/interface.py](backend/agents/llm_service/interface.py) — extend `LLMJsonParseError.__init__`.
- New: `backend/agents/llm_service/tests/test_structured_output.py`

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
