# Audit: The 5 LLM-Calling Patterns in `software_engineering_team`

## Status

**Draft — audit only.** This document enumerates the coexisting LLM-calling
patterns in `software_engineering_team` and states each one's
error-propagation/failure-handling behavior explicitly, so a follow-up
decision can pick a canonical default for new agents. **Deciding that
default is out of scope here** and is tracked in a sibling sub-issue of
"Document a canonical LLM-calling pattern for new SE-team agents."

## Context

`software_engineering_team` has grown five distinct "how do I call the LLM
and what happens when it fails" idioms, introduced independently as
different sub-teams (`code_v2`, `devops_team`, the top-level persona agents,
`ai_agent_development_team`, and the original hand-rolled agents) were built
or migrated at different times. Each idiom is internally consistent and
documented in its own module, but the five disagree with each other on the
questions that matter most when an LLM call fails:

- Does the failure **raise** to the caller, or does it **degrade to a safe
  fallback value**?
- Is the fallback a **generic empty/failed shape**, or a **type-specific
  default the caller must construct itself**?
- Does the pattern **retry** before giving up, and with what backoff?
- Is JSON-parse failure treated the **same as** or **differently from** an
  LLM call/network failure?

This document is organized as one section per pattern: what it is, which
agents use it, and — in a "Failure handling" subsection — the explicit
answers to those four questions.

---

## Pattern 1 — `LlmToolAgentBase` (opt-in shared base, config-driven)

**Module:** `shared/llm_tool_agent_base.py`

The dependency-light base shared by every code-v2 "tool agent" family. It is
not one fixed behavior but a set of **opt-in, class-attribute-parameterized
steps** that subclasses mix and match: model resolution
(`resolve_models`), LLM invocation (`use_run_strands_agent`: inline
`str(agent(prompt)).strip()` vs. `run_strands_agent`), JSON-parsing strategy
(`json_parse_strategy`: `"lenient"` → `{}` on failure, vs. `"extract"` →
`None` on failure), and a set of **call-site opt-in fallback helpers**
(`_fallback_no_model`, `_call_with_single_fallback`,
`_call_partial_tolerant`, `_fallback_empty_parse`) that are never
auto-wired — a subclass must explicitly call them.

Two concrete recipes ship on top of it:

- **`BaseReviewToolAgent` / `ReviewToolAgent`** (`shared/tool_agent_base.py`)
  — the "review + single-issue fix" shape used by the security, testing/QA,
  accessibility, performance, UX, and build-specialist tool agents in both
  `backend_code_v2_team` and `frontend_code_v2_team` (e.g.
  `backend_code_v2_team/tool_agents/api_openapi/agent.py`,
  `frontend_code_v2_team/tool_agents/security/agent.py`).
- **`JsonGeneratorToolAgent`** (`ai_agent_development_team/tool_agents/_base.py`)
  — the Plan/Json recipe (`response_format="json"`,
  `json_parse_strategy="extract"`, inline invocation) used by the six
  `ai_agent_development_team` domain tool agents (`agent_runtime`,
  `safety_governance`, `evaluation_harness`, `memory_rag`,
  `prompt_engineering`, `mcp_server_connectivity`).

**Failure handling.**

- **No model configured** — `_fallback_no_model` returns a
  `FallbackPayload(tier="no_model", ...)` (or, in `BaseReviewToolAgent.review`,
  a `"skipped (no LLM)"` summary with no issues) instead of calling the LLM
  at all. Never raises.
- **LLM call raises** — `_call_with_single_fallback` catches `Exception`,
  logs a warning on the *subclass module's* logger, and returns
  `("error", FallbackPayload(tier="call_error", ...))`. In
  `BaseReviewToolAgent.review` this becomes a `"failed (LLM error)"` summary
  with no issues. In `JsonGeneratorToolAgent.run`, a call-error also flips
  `ToolAgentOutput.success = False` (so the orchestrator marks the microtask
  `FAILED` instead of silently treating an outage as complete). **No
  built-in retry** — one call, one catch.
- **JSON parse fails** — `"lenient"` strategy returns `{}` (parsed as "0
  issues found", logged at warning); `"extract"` strategy returns `None`.
  Malformed-but-received output is *not* treated as a call failure: in
  `JsonGeneratorToolAgent`, an empty/unparseable JSON object still returns
  `success=True` (the historical default) — only a missing model or a raised
  exception sets `success=False`. This is an intentional split: "the model
  answered but didn't give me usable JSON" is not the same failure class as
  "I couldn't reach/run the model."
- **Partial-failure tolerance** — `_call_partial_tolerant` (used by
  `BaseReviewToolAgent.problem_solve`, which fixes issues one at a time) logs
  and skips any single item whose `fn(item)` raises, returning only the
  successes; one bad fix attempt does not abort the batch.
- **Net effect:** this pattern never lets an LLM/parse failure propagate out
  of `run`/`review`/`problem_solve`/`plan`/`deliver` — every failure mode
  degrades to a `ToolAgentOutput`/`ToolAgentPhaseOutput` shape, with the
  no-model/call-error/empty-parse "tiers" distinguishable in the returned
  payload for callers that care.

---

## Pattern 2 — `run_structured_persona` (Strands `structured_output_model`)

**Module:** `shared/persona_agent_base.py`

A single function, not a class hierarchy. Callers build a fresh Strands
`Agent` per call (required: reusing one `Agent` instance across calls breaks
`structured_output_model`'s forced tool choice, since Strands accumulates
message history), call it with `structured_output_model=<Output type>`, and
supply a `fallback_factory` that turns any exception into a safe, final
instance of that output type.

**Call sites:** the four top-level persona agents —
`security_agent/agent.py` (`CybersecurityExpertAgent`), `qa_agent/agent.py`,
`accessibility_agent/agent.py`, `integration_team/agent.py`. Each keeps its
own log messages, system prompt, and fallback field values as call-site
data; `run_structured_persona` is the shared "build → call → coerce-or-fallback"
scaffold, previously duplicated verbatim four times.

**Failure handling.**

- **Any failure** — building the agent, calling it, or the returned
  `structured_output` not being an instance of `output_model` — is caught by
  a single broad `except Exception` inside `run_structured_persona` and
  routed to `fallback_factory(exc)`. **Never raises** to the caller.
- The fallback is **type-specific and caller-authored**, not a generic empty
  shape: e.g. `security_agent`'s `_fallback` returns a
  `SecurityOutput(vulnerabilities=[], approved=False, summary=f"Security
  analysis failed: {exc}", ...)` — a deliberately unapproved, safe-by-default
  result.
- **No retry** — one call, one catch. (Retry, if any, would have to happen
  at a layer above the persona agent's `run`.)
- **Fallback bypasses `on_success`** — this is a deliberate, documented
  asymmetry: callers often derive a pass/fail flag from the *reported
  findings* inside `on_success` (e.g. "approved iff no critical/high
  severities"), and an empty findings list from the safe fallback must never
  be reinterpreted as a clean approval by re-running that same
  derivation. `on_success` only ever sees a genuine model result.
- JSON-parsing is not a separate step here — validation is delegated to
  Strands' forced structured-output mechanism, so "the model answered but
  the shape was wrong" and "the call failed" are both funneled into the same
  `except Exception` branch, unlike Pattern 1's explicit no-model/call-error/
  empty-parse tiers.

---

## Pattern 3 — `DevOpsSingleShotAgent` (config-driven, propagating)

**Module:** `devops_team/_agent_template.py`

The devops-team counterpart to Pattern 1, but with the **opposite** failure
philosophy: it is a template around `complete_json_with_continuation`
(`shared/llm.py`) with an optional `pre_call` short-circuit hook, and
subclasses only override `build_context` and `build_output`.

**Call sites:** `devops_team/cicd_pipeline_agent/agent.py`,
`devops_team/deployment_strategy_agent/agent.py`,
`devops_team/iac_agent/agent.py` (`InfrastructureAsCodeAgent`), and other
single-shot JSON devops agents.

**Failure handling.**

- **LLM/parse errors propagate unchanged.** `run()` has no `try`/`except`
  around the `complete_json_with_continuation` call: any exception raised by
  that call — including `LLMJsonParseError` when even the fenced/prose
  recovery in `extract_json_from_response` fails — is raised straight out of
  `run()` to whatever called the devops agent (the devops orchestrator, which
  owns its own patch/retry loop — see `orchestrator.py`'s Phase 4.6
  debug-patch loop, which does wrap its calls in `try`/`except Exception`).
  This pattern **does not** build a safe fallback output itself.
- **Retry/continuation** happens one layer down, inside
  `complete_json_with_continuation` and the underlying LLM client, not in
  `DevOpsSingleShotAgent` itself — continuation-on-truncation is
  transparent to this pattern, but a truncation that survives continuation
  still raises.
- **`pre_call`** is the only built-in way to avoid calling the LLM at all;
  when it returns non-`None`, `run()` returns that value directly.
- **Explicit standardization decision recorded in the module docstring:**
  `complete_json_with_continuation` is the canonical helper for devops
  single-shot agents; moving devops onto Pattern 2
  (`run_structured_persona`) was considered and deferred because several
  devops outputs carry nested models (`DevOpsCompletionPackage`,
  `IaCExecutionError`, `ReviewFinding`) that would need verification against
  Strands' `structured_output_model` mechanism first.

---

## Pattern 4 — `FileGeneratorToolAgent` / `StubToolAgent` (static-phase bases)

**Module:** `shared/tool_agent_static.py`

Two related bases under the shared `StaticPhaseToolAgent` lifecycle
template, whose `plan`/`review`/`problem_solve`/`deliver` phases always
return static, config-driven advisory output (no LLM call in those phases
at all):

- **`FileGeneratorToolAgent`** — subclasses (auth, data-engineering,
  API/OpenAPI generators in both `backend_code_v2_team/tool_agents/` and
  `frontend_code_v2_team/tool_agents/`) run exactly **one** LLM prompt in
  `execute` and parse `## FILE ## / ## SUMMARY ##` template output via a
  team-specific `_parse_files_and_summary` hook.
- **`StubToolAgent`** — adapter stubs (e.g. CI/CD, containerization —
  `backend_code_v2_team/tool_agents/static_agents.py`) whose `execute` never
  calls an LLM at all; every phase returns a static "not yet implemented"
  message.

**Failure handling.**

- **`StubToolAgent`** never calls an LLM, so there is nothing to fail —
  N/A.
- **`FileGeneratorToolAgent.execute`** has **no `try`/`except`** around
  either the LLM call (`str(self._agent_factory()(model=self._model)(prompt)).strip()`)
  or the template parse (`self._parse_files_and_summary(raw)`): both are
  hand-rolled, uncaught calls, so any exception from either — a connection
  error, a rate limit, or a parser bug on unexpected model output —
  **propagates to the caller unchanged**. Unlike Pattern 1's
  `BaseReviewToolAgent` (which the same module family sits beside), there is
  no no-model/call-error fallback tier here at all: this base assumes the
  caller (the phase orchestrator) owns retry/fallback policy for generator
  failures.
- **No retry** built into this pattern.

---

## Pattern 5 — Hand-rolled call sites (heterogeneous, per-call-site policy)

Outside the four shared bases/helpers above, a substantial number of agents
build a Strands `Agent` and call it directly, each with its **own,
independently-authored** error-handling policy. This is a pattern only in
the sense that "no shared helper is used" — the actual failure behavior
varies call site to call site, which is itself the main finding: a new
contributor has at least four *different* hand-rolled precedents to copy
from, not one.

Representative call sites and their distinct behaviors:

- **`tech_lead_agent/agent.py`** — the Task Graph engine's Tech Lead. Its
  `_call_json` helper (used by `run_plan_to_task_graph`, `run_groom_task`,
  etc.) wraps the call in `call_llm_with_retries` (exponential backoff,
  `max_attempts=_review_retry_attempts()`) and, on any exception surviving
  retries, **logs a warning and returns a caller-supplied `default` dict**
  — never raises. A `retries=False` mode exists for call sites (like
  `run_revision_adjudication`) that wrap their own outer retry/backoff and
  need the raw exception; those catch at the outer layer instead and return
  a **domain-specific fail-closed verdict** (`{"verdict": "fail", "reason":
  ...}`) rather than a generic empty dict — a stuck task that cannot even be
  adjudicated must terminate with a diagnostic, not re-enter the loop.
- **`shared/decomposition.py`** (`DecompositionEngine.process`) — catches
  specifically `LLMTruncatedError` (not `Exception`) and responds with
  content decomposition + recursive retry on smaller chunks, up to
  `max_depth`; if decomposition is exhausted while still truncated, it
  writes a post-mortem artifact and **re-raises**. Any *other* exception
  (a non-truncation LLM error, a JSON-parse failure via
  `extract_json_from_response`) is **not caught at all** and propagates
  immediately — this pattern treats truncation as the one specially-handled
  failure mode and everything else as the caller's problem.
- **`code_review_agent/synthesis.py`** (`synthesize_review_findings`) —
  wraps the entire call (prompt build, agent call, JSON parse, key
  validation) in one broad `try/except Exception`, logs a warning per
  distinct failure reason, and returns **`None`** on any failure — by
  contract, never raises. The caller (the review coordinator) is documented
  to fall back to deterministic concatenation of per-chunk summaries when it
  gets `None`, so a synthesis failure degrades review *narrative* quality
  without affecting the deterministic pass/fail verdict.
- **`code_review_agent/false_positive_filter.py`** — similarly fail-safe by
  explicit contract ("a failure must keep findings, not drop them"): reader
  and verification failures are caught and swallowed, defaulting to
  returning the original, unfiltered findings list rather than either
  raising or silently dropping data.
- **`architect_agents/agents/*.py`** (e.g. `api_design.py`,
  `application.py`, `security.py`, `devops.py`, …) — Strands `@tool`
  functions invoked by an orchestrating architect agent. These have **no
  `try`/`except` at all**: `agent = Agent(...); result = agent(context);
  return str(result)`. Any exception — LLM failure, tool failure inside the
  sub-agent — propagates straight out of the `@tool` call and is handled (or
  not) by whatever Strands/orchestrator machinery invoked the tool. This is
  the most "raw" of the five patterns: zero local failure handling.
- **`build_fix.py` / `build_fix_specialist/agent.py`** — build/patch-loop
  helpers that wrap individual LLM calls in `try/except Exception`, log, and
  return a `(False, summary)` failure tuple rather than raising — by
  contract ("Neither function raises into the build gate") the build gate
  itself never sees an exception, only a boolean outcome plus a message.

**Failure handling, summarized:** unlike Patterns 1–4, there is no single
answer for "does it raise or fall back, and to what" — it must be looked up
per call site. The catalogued sites above span the full spectrum: always
raises (`architect_agents`), always swallows to a generic sentinel
(`synthesis.py` → `None`, `build_fix.py` → `(False, summary)`), swallows to
a domain-specific fail-closed value (`tech_lead_agent`'s adjudication
verdict), and conditionally raises depending on the exception's *type*
rather than its origin (`decomposition.py`'s truncation-only catch).

---

## Summary Table

| Pattern | On LLM call failure | On JSON/parse failure | Retry? | Fallback shape |
|---|---|---|---|---|
| 1. `LlmToolAgentBase` | Caught, returns tiered `FallbackPayload` / `success=False` | Distinct tier: `{}` (lenient) or `None` (extract); does *not* imply `success=False` in `JsonGeneratorToolAgent` | No (single attempt) | Generic, tier-labeled |
| 2. `run_structured_persona` | Caught (broad `except Exception`), routed to `fallback_factory` | Folded into the same broad catch (Strands structured-output validation failure) | No | Type-specific, caller-authored, safe-by-default |
| 3. `DevOpsSingleShotAgent` | **Propagates unchanged** | **Propagates unchanged** (`LLMJsonParseError` after recovery attempts) | Continuation only, inside `complete_json_with_continuation`; no top-level retry | None — caller must handle |
| 4. `FileGeneratorToolAgent` | **Propagates unchanged** | **Propagates unchanged** | No | None — caller must handle |
| 5. Hand-rolled | **Varies per call site** (raises / generic sentinel / domain-specific fail-closed value, depending on file) | **Varies per call site** | **Varies per call site** | **Varies per call site** |

## Open questions for the canonical-default decision (tracked separately)

- Should the canonical default **propagate** (Patterns 3/4/most of 5) or
  **degrade to a safe fallback** (Patterns 1/2/some of 5)? The codebase
  currently has real, documented reasons for both: propagate when the
  caller already owns a retry/patch loop (devops orchestrator,
  `tech_lead_agent`'s outer retry), degrade when the call site is a leaf
  microtask output that must always produce *some* result.
- Should JSON-parse failure be distinguishable from call failure in the
  return shape (Pattern 1's tiers) or collapsed into one failure class
  (Pattern 2, most of Pattern 5)?
- Where should retry-with-backoff live — inside the shared helper (none of
  the five do this uniformly today; `call_llm_with_retries` is invoked ad
  hoc from `tech_lead_agent` and available but unused elsewhere) or always
  at the caller?
