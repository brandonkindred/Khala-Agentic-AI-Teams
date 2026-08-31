# Strategy Lab — Diff-Prompt Reconstruction Fidelity

`RefinementAgent.run()` (via `_diff_format.diff_or_full`) and
`DesignAgent.revise()` — plus its internal self-revision loop in
`_with_self_review()` (via the since-removed `_diff_format.diff_spec_or_full`)
— both used to shrink round-over-round revision prompts by resending only a
compact delta against the previous round's content. Both agents'
`_invoke_and_parse` deliberately builds a **fresh, history-free**
`strands.Agent` per round — reusing one `Agent` would feed the model its own
unparseable output back as context, defeating the correction-retry contract.
That deliberate choice has a cost: when a diff-only section is sent, the
model has no independent copy of the untouched prior content. It must either
reconstruct it from context alone or invent it — and the response schema
still requires "the complete fixed code" / "the complete revised
specification" back, so a dropped or hallucinated field is indistinguishable
from an intentional edit unless something downstream catches it.

The two agents carry asymmetric risk from that same tradeoff, and are
handled differently as a result.

## `RefinementAgent` — kept, risk is bounded

`RefinementAgent.run()`'s "## Current Code" section diffs `strategy_code`
against the previous round when the diff renders smaller (`diff_or_full`,
falling back to the full file on round 1 or a near-total rewrite). This is
safe to keep because:

- The governing `StrategySpec` fields (entry/exit/sizing rules, risk limits,
  hypothesis) are **always** rendered in full in the "## Current Strategy"
  section of every refinement prompt, regardless of diffing — only the
  *derived* `strategy_code` artifact is ever diffed.
- A botched reconstruction of `strategy_code` is not the last checkpoint: it
  still has to pass code-conformance gates, execution, and the backtest
  before it reaches a record. A silently dropped or altered code fragment
  fails loudly there rather than propagating unnoticed.

No change was made here.

## `DesignAgent` — diffing removed, always sends the full spec

`DesignAgent.revise()`'s "## Current Specification" section is different in
kind, not degree: the spec dict it sends *is* the authoritative object the
model must fully reconstruct and return — there is no separately-rendered
full-fidelity anchor the way `RefinementAgent` has in "## Current Strategy".
Tracing the call site (`orchestrator_design.py`'s
`_revise_with_regression_notice`) confirms there is also no downstream
safety net: the returned dict flows straight into `build_spec_from_dict`,
which only does shape/type coercion and asset-class-alias resolution, and
`SpecReadinessGate` validates the resulting spec in isolation — neither
compares the new spec field-by-field against the real prior `StrategySpec`
object the orchestrator still holds at that point. A hallucinated or
silently dropped field (a risk limit, a target symbol, an entry/exit rule)
would pass straight through undetected and corrupt the strategy's actual
semantics — exactly the "no downstream cross-check" case a diff-based
revision prompt cannot afford.

Existing test coverage for the diff-wiring behavior only exercised prompt
*shape* against a scripted/mocked `_invoke_and_parse` — it could assert what
prompt text was sent, but not whether a real model would reconstruct the
untouched fields correctly from a diff. That gap is exactly what motivated
this decision rather than trying to test around it: a fix that removes the
failure mode is cheaper and more certain than a test that could only ever
observe it indirectly.

**Decision**: `DesignAgent.revise()` and the self-revision loop inside
`_with_self_review()` now always render the full spec JSON in "## Current
Specification" — never a diff. There is no round-over-round diff state left
in `DesignAgent` (no `_previous_round_spec`, no per-loop diff base); every
round is prompted from the actual spec dict in hand. This trades some prompt
size for a guarantee that the model is never asked to reconstruct a field it
cannot see. `_diff_format.diff_spec_or_full` (and its private helpers,
`_walk_dict_diff` / `_values_differ` / `_leaf_lines`) were removed along with
their only production call site rather than left as dead code; `diff_or_full`
(used by `RefinementAgent`) is unaffected.

## Locked in by

| Contract | Tests |
| --- | --- |
| `diff_or_full` behavior (no previous round, small diff, near-total rewrite, edge cases) | `tests/test_strategy_lab_diff_format.py` |
| `RefinementAgent.run()` diffs `strategy_code` round-over-round; governing spec fields always render in full | `tests/test_strategy_lab_refinement_diff_wiring.py` |
| `DesignAgent.revise()` and the internal self-revision loop always send the full spec JSON, on every round, including after a failed invocation | `tests/test_strategy_lab_design_full_reconstruction.py` |
