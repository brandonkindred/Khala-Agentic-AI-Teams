# Decision: Canonical LLM-Calling Pattern for New SE-Team Agents

## Status

**Decided.** This document states which of the five coexisting
"prompt-build → call LLM → parse → fallback" patterns is the default for
**new** `software_engineering_team` agents, and documents each other
pattern as a justified exception. It builds on
[`docs/LLM_CALLING_PATTERNS_AUDIT.md`](LLM_CALLING_PATTERNS_AUDIT.md),
which is the source of truth for each pattern's detailed
error-propagation/failure-handling behavior — this document does not
repeat that detail, only references it.

## Decision

**Default for new agents: Pattern 1 — `LlmToolAgentBase`**
(`shared/llm_tool_agent_base.py`).

A new agent should subclass whichever of the three existing concrete
recipes matches its shape, rather than reimplementing the base:

| New agent shape | Subclass | Location |
|---|---|---|
| Review-and-fix a piece of code/output | `BaseReviewToolAgent` / `ReviewToolAgent` | `shared/tool_agent_base.py` |
| Generate a JSON plan/spec from a prompt | `JsonGeneratorToolAgent` | `ai_agent_development_team/tool_agents/_base.py` |
| Produce a plan with recommendations/summary | `PlanGeneratorToolAgent` | `frontend_code_v2_team/tool_agents/_plan_base.py` |

If none of these three recipes fit, compose new call-site behavior out of
`LlmToolAgentBase`'s opt-in helpers (`_fallback_no_model`,
`_call_with_single_fallback`, `_call_partial_tolerant`,
`_fallback_empty_parse`) rather than hand-rolling a new pattern from
scratch (see Pattern 5, below, on why hand-rolling is not an option for new
work).

## Rationale

- **Adoption breadth.** It's already the most widely used pattern (roughly
  16 of 53 agents, across three independently-evolved recipes), so a new
  agent following it has the most precedent to copy from and the most
  reviewer familiarity to draw on.
- **Config-driven fallback instead of duplicated logic.** The no-model /
  call-error / empty-parse tiers are reusable class-attribute-driven
  behavior, not logic a new agent has to write and get right itself.
- **Call failure vs. parse failure are distinguishable.** The pattern
  treats "the model answered but the JSON was unusable" as a different
  failure class from "I couldn't reach/run the model at all" — useful
  signal for an orchestrator deciding whether a retry is likely to help.
- **DbC alignment.** The tiered fallback shape gives a new agent an
  explicit, inspectable contract for what happens on failure, consistent
  with this repository's Design by Contract requirement — versus Pattern
  5's per-call-site, unstated policy.

## Known caveats

Per this repository's rule that a contract must be stated explicitly, new
agents adopting Pattern 1 should be aware of two gaps the audit already
found in the pattern's current implementation. These are **not** fixed by
this decision — fixing them is out of scope here (see below) — but a new
agent's author should know the "never lets a failure propagate" guarantee
is not exhaustive:

- **Non-dict-but-valid JSON is not handled.** `BaseReviewToolAgent.review`
  only special-cases a missing/empty/dict-shaped payload; if the model
  returns a syntactically valid JSON array instead of an object, `(data or
  {}).get(...)` raises an uncaught `AttributeError` straight out of
  `review()`.
- **`problem_solve`'s per-item loop doesn't cover parse failures.**
  `BaseReviewToolAgent.problem_solve`'s hand-rolled per-item loop only
  wraps the LLM call in `try`/`except`, not the subsequent
  `_parse_single_issue` call — a parser bug on one item aborts the entire
  batch instead of being tolerated per-item, unlike what the sibling
  `_call_partial_tolerant` helper would provide if it were wired in there.

## Each other pattern as a justified exception

### Pattern 2 — `run_structured_persona`

Justified for the three existing top-level persona agents (QA,
accessibility, integration): Strands' `structured_output_model` forced
tool-choice mechanism and a caller-authored, safe-by-default typed
fallback are a good fit for a single top-level agent producing one typed
result per call. A **new** single top-level persona-style agent may
reasonably choose this pattern instead of Pattern 1 if its shape — one
agent, one structured output type, one caller-owned fallback value — fits
it better. See the audit's Pattern 2 section for the exact
construction-vs-call exception boundary.

`security_agent` (`CybersecurityExpertAgent`) was originally a fourth
Pattern 2 caller but has since migrated to `run_single_shot_review`
(below, schema-validated mode) to gain a bounded corrective retry on a
malformed reply, which Pattern 2 does not offer. That migration is a
change to an *existing* agent, not a new agent's pattern choice, so it
falls under this document's own "migrating any existing agent between
patterns" out-of-scope carve-out rather than the "no new hand-rolled call
sites" rule below (Pattern 5), which is scoped to new agents.

### Pattern 3 — `DevOpsSingleShotAgent`

Justified by its own module docstring's existing decision record: devops
outputs carry nested models (`DevOpsCompletionPackage`,
`IaCExecutionError`, `ReviewFinding`, etc.) that haven't been verified
against Strands' `structured_output_model` mechanism, and
`devops_team/phase2_graph.py`'s `run_phase2_parallel` already owns the
catch/fallback/retry policy one layer above the agent template. The
template itself propagating failures unchanged is intentional, not an
oversight — remains the pattern for devops single-shot agents until that
nested-model verification work happens.

### Pattern 4 — `FileGeneratorToolAgent` / `StubToolAgent`

Justified because it's a static-phase lifecycle base
(`StaticPhaseToolAgent`) where the phase orchestrator, not the tool agent,
owns retry/fallback policy for the single LLM call
`FileGeneratorToolAgent.execute` makes. Adding a fallback tier inside the
tool agent itself would duplicate policy the orchestrator already owns.

### Pattern 5 — Hand-rolled call sites

Existing call sites are each individually justified by their own
documented, working policy — e.g. `tech_lead_agent`'s retry-then-default
via `agent_call_json`/`call_llm_with_retries`, `decomposition.py`'s
truncation-specific recursive retry, `synthesis.py`'s
fail-open-to-`None` contract, `false_positive_filter.py`'s
never-drop-findings contract. These are grandfathered because migrating
them is explicitly out of scope (see below).

**New agents should not add new hand-rolled call sites.** Pattern 5 is not
an option going forward — it exists as a record of pre-decision code, not
as a menu choice for new work.

### `run_single_shot_review` — shared single-shot `LLMClient` helper

`shared/single_shot_review.py`'s `run_single_shot_review(llm_client,
agent_key, prompt, system_prompt, *, schema=..., ...)` is justified as a
narrow exception distinct from Patterns 1-5 above: it is a plain function,
not an agent, so it has no review/generate/plan-specific shape to subclass
into any of Patterns 1-4's recipes. It exists to give already-hand-rolled
(Pattern 5) call sites — and any narrowly-scoped future ones with the same
shape — one shared implementation of the "resolve an `LLMClient` for
`agent_key` unless one is already injected, then make one single-shot call"
boilerplate they currently duplicate by hand, rather than each repeating
it. **It is not a sixth menu option for a new agent's overall calling
shape** — new agents still default to Pattern 1 (or its justified
exceptions 2-4) exactly as stated above; this helper is for call sites that
need the underlying single-shot `LLMClient` call itself, e.g. a future
migration of existing Pattern 5 sites onto one shared implementation
instead of N duplicated ones (tracked separately, not part of this
helper's introduction).

**Relationship to `llm_service.api.generate_structured`.** The
schema-validated branch delegates to `generate_structured`
(`llm_service/api.py`) rather than calling `complete_validated` itself.
`generate_structured` was extended with an optional `llm_client` parameter
(used as-is when given, instead of always resolving one via
`get_client(agent_key)`) plus `think`/`context`/`**kwargs` forwarding, so
it can now serve `run_single_shot_review`'s dependency-injection callers
too — the canonical public entrypoint owns the resolve+validate contract
in both places instead of two parallel implementations of it. The
plain-JSON branch still has no canonical entrypoint to delegate to (`schema`
is a required parameter of `generate_structured`), so it resolves a client
and calls `complete_json` directly, as documented above.

`security_agent/agent.py` (`CybersecurityExpertAgent`) is the first such
migration: it now calls `run_single_shot_review` in schema-validated mode
(`schema=SecurityLLMResponse`) in place of its former Pattern 2
(`run_structured_persona`) call, gaining the bounded corrective retry
Pattern 2 doesn't offer.

## Out of scope

- Migrating any existing agent between patterns.
- Fixing the two Pattern 1 gaps noted above (may be tracked as a future
  issue; not part of this decision).

## See also

- [`docs/LLM_CALLING_PATTERNS_AUDIT.md`](LLM_CALLING_PATTERNS_AUDIT.md) —
  full audit of all five patterns' failure-handling behavior.
