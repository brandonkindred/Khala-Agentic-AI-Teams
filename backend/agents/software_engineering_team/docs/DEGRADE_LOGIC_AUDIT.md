# Degrade Logic Audit — SE-Team Gate Agents

**Issue:** #6936  
**Parent Story:** #6931  
**Date:** 2026-08-21  
**Related docs:**
[`LLM_CALLING_PATTERNS_AUDIT.md`](LLM_CALLING_PATTERNS_AUDIT.md) (Pattern 2
call-site details),
[`LLM_CALLING_PATTERN_DECISION.md`](LLM_CALLING_PATTERN_DECISION.md)
(canonical pattern decision and Security's justified exception)

## Purpose

Audit the 4 existing SE-team gate agents' "try structured output, degrade on
failure" call sites, identify semantic differences in their exception
handling, logging, and fallback behavior, and define the canonical
`try_structured_or_degrade` contract that a shared helper must satisfy.

This document supplements — not restates — the existing Pattern 2 analysis
in [`LLM_CALLING_PATTERNS_AUDIT.md`](LLM_CALLING_PATTERNS_AUDIT.md). That
audit covers the Strands `structured_output_model` mechanism and
`run_structured_persona`'s overall failure-handling shape. This document
focuses narrowly on the **degrade semantics** (what happens after the
structured call fails) and the **differences between the 4 sites** that a
unified helper must reconcile.

---

## 1. Call-Site Comparison Table

For full call-site construction details (agent-factory reuse constraints,
system-prompt composition, etc.) see
[`LLM_CALLING_PATTERNS_AUDIT.md § Pattern 2`](LLM_CALLING_PATTERNS_AUDIT.md).
The table below focuses on degrade-specific dimensions only.

| Dimension | QA Agent | Security Agent | Accessibility Agent | Integration Agent |
|-----------|----------|----------------|---------------------|-------------------|
| **Helper used** | `run_structured_persona` | Direct `try/except` around `run_single_shot_review` | `run_structured_persona` | `run_structured_persona` |
| **Underlying LLM mechanism** | Strands Agent + `structured_output_model` | `generate_structured` (LLMClient API) with `correction_attempts=1` | Strands Agent + `structured_output_model` | Strands Agent + `structured_output_model` |
| **Exception types caught** | All (`Exception`) via helper | All (`Exception`) inline | All (`Exception`) via helper | All (`Exception`) via helper |
| **Logging** | `logger.warning("QA: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Security: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Accessibility: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Integration: structured_output failed (%s); returning failed result", exc)` |
| **Fallback output** | `QAOutput(bugs_found=[], approved=False, quality_gates={"acceptance_evidence":"fail"} if acceptance mode else {}, summary=f"...{exc}")` | `SecurityOutput(vulnerabilities=[], approved=False, summary=f"...{exc}", remediations=[])` | `AccessibilityOutput(issues=[], approved=False, summary=f"...{exc}")` | `IntegrationOutput(passed=False, issues=[], summary=f"...{exc}", fix_task_suggestions=[])` |
| **Retry / correction logic** | Strands implicit: unbounded correction cycles (no turn cap — each invalid tool-use triggers another model cycle) | 1 explicit self-correction attempt inside `generate_structured`, then exception propagates | Same as QA (Strands implicit, unbounded) | Same as QA (Strands implicit, unbounded) |
| **Post-success processing** | `_finalize`: re-derives `approved` from severities/gates; mode-dependent | Inline: re-derives `approved` via `derive_approved` | `_finalize`: re-derives `approved` from severities | `_finalize`: re-derives `passed` from severities |
| **Cache interaction** | Fallback results NOT cached (`is_fallback` flag) | Fallback results NOT cached (early-return before cache-write) | No caching | No caching |
| **Fail-closed guarantee** | Yes (`approved=False` + explicit gate fail in acceptance mode) | Yes (`approved=False`) | Yes (`approved=False`) | Yes (`passed=False`) |

---

## 2. Semantic Differences Identified

### Difference A: Underlying LLM Dispatch Mechanism

- **3 agents** (QA, Accessibility, Integration) use Strands Agent +
  `structured_output_model` with unbounded implicit correction cycles.
- **Security** uses `run_single_shot_review` → `generate_structured` with
  `correction_attempts=1` (bounded to one explicit retry).

See [`LLM_CALLING_PATTERN_DECISION.md`](LLM_CALLING_PATTERN_DECISION.md)
for the full justification of Security's migration to
`run_single_shot_review`.

**Decision:** UNIFY. The canonical `try_structured_or_degrade` helper
supports both backends by accepting a generic `call: Callable[[], OutputT]`.
The Strands-based agents pass their `run_structured_persona` invocation as
`call`; Security passes its `run_single_shot_review` invocation. The helper
is backend-agnostic — it only cares about the call's success/failure
outcome, not how the underlying LLM dispatch works.

### Difference B: Logging Message Wording

- QA/Security/Accessibility: "returning fallback"
- Integration: "returning failed result"

**Decision:** UNIFY. The helper emits a standardized log line:
`"{agent_name}: structured_output failed ({exc}); degrading to safe
fallback"`.

### Difference C: Mode-Dependent Fallback Fields (QA Only)

- QA's fallback varies by mode (`quality_gates={"acceptance_evidence":"fail"}`
  in acceptance mode).
- Other agents have uniform fallback shapes.

**Decision:** PRESERVE via `fallback_factory`. Each agent supplies its own
factory that knows its shape/mode. The helper does not need to understand
modes.

### Difference D: Post-Success Processing (`on_success` / `_finalize`)

- All 4 agents re-derive pass/fail from reported findings.
- Logic differs in field names and QA's mode-conditional gate logic.

**Decision:** PRESERVE via `on_success` callback. The helper calls
`on_success(result)` on the happy path and skips it for fallback results.

### Difference E: Self-Correction Retry Budget

- Security: bounded (1 explicit attempt = at most 2× cost).
- Strands-based: unbounded (potentially multiple cycles with compounding
  latency/cost; no turn cap in Strands ≥1.52.0).

**Decision:** PRESERVE. Correction/retry is an implementation detail of the
`call` callable that happens before any exception reaches the helper's
catch boundary. The helper never retries — it sees only the final
success-or-failure outcome.

### Difference F: Cache Bypass on Fallback

- QA and Security avoid caching fallback results.
- Accessibility and Integration have no caching.

**Decision:** PRESERVE as caller responsibility. The helper returns
`is_fallback` so callers can decide whether to cache.

---

## 3. Canonical `try_structured_or_degrade` Contract

### Signature

```python
from typing import Any, Callable, TypeVar

OutputT = TypeVar("OutputT")

def try_structured_or_degrade(
    *,
    call: Callable[[], OutputT],
    fallback_factory: Callable[[Exception], OutputT],
    on_success: Callable[[OutputT], OutputT] | None = None,
    agent_name: str = "",
    logger: Any = None,
) -> tuple[OutputT, bool]:
    """Try a structured-output call; degrade to a safe fallback on any failure.

    Parameters
    ----------
    call
        Zero-argument callable that performs the structured LLM call and
        returns a validated output instance. This is where the agent places
        its Strands Agent invocation (via run_structured_persona) or its
        run_single_shot_review call — the helper is backend-agnostic.
    fallback_factory
        Called with the caught exception; must return a safe, final output
        instance (e.g. approved=False, empty findings). Must not raise.
    on_success
        Optional post-processing hook applied to a successful result before
        returning. Skipped for fallback results (those are already final).
        If on_success itself raises, the exception is caught and routed to
        fallback_factory.
    agent_name
        Human-readable agent label for the log message (e.g. "QA",
        "Security").
    logger
        Logger instance. If provided, emits a warning on fallback.

    Returns
    -------
    tuple[OutputT, bool]
        (result, is_fallback) — the output instance and whether it came
        from the fallback path. Callers use is_fallback to decide whether
        to cache the result, emit metrics, etc.

    Behavior Contract
    -----------------
    1. Calls call().
    2. On success, returns (on_success(result), False) — or (result, False)
       if on_success is None.
    3. On any Exception from call() OR from on_success():
       a. Logs: "{agent_name}: structured_output failed ({exc}); degrading
          to safe fallback"
       b. Returns (fallback_factory(exc), True).
    4. Exceptions from fallback_factory propagate (programming error).
    5. The helper NEVER retries. Retry logic belongs inside call().
    6. Catches broad Exception (not BaseException) — KeyboardInterrupt,
       SystemExit, etc. propagate.
    """
```

### Behavior Contract Summary

| Step | Happy Path | Failure Path |
|------|-----------|--------------|
| 1. Invoke `call()` | Returns `OutputT` | Raises `Exception` |
| 2. Apply `on_success` | Returns `OutputT` | Raises → step 3 |
| 3. Log warning | — | `logger.warning(...)` |
| 4. Return | `(result, False)` | `(fallback_factory(exc), True)` |

### Implementation Strategy

`try_structured_or_degrade` is a backend-agnostic degrade primitive.
`run_structured_persona` can be reimplemented in terms of it (note: the
return type changes from `OutputT` to `tuple[OutputT, bool]` — this is a
**breaking change** that requires updating all 3 callers atomically):

```python
import logging as _logging

_logger = _logging.getLogger(__name__)

def run_structured_persona(
    *, model, system_prompt, user_prompt, output_model,
    fallback_factory, agent_factory, on_success=None,
    system_prompt_content=None, agent_name="",
) -> tuple[OutputT, bool]:
    def _call() -> OutputT:
        composed = build_system_prompt_with_content(system_prompt, system_prompt_content)
        agent = agent_factory(model=model, system_prompt=composed)
        result = agent(user_prompt, structured_output_model=output_model)
        output = result.structured_output
        if not isinstance(output, output_model):
            raise TypeError(f"Expected {output_model.__name__}, got {type(output).__name__}")
        return output

    return try_structured_or_degrade(
        call=_call,
        fallback_factory=fallback_factory,
        on_success=on_success,
        agent_name=agent_name,
        logger=_logger,
    )
```

This is a **breaking signature change** — callers must be updated to
destructure the tuple. All 3 `run_structured_persona` call sites (QA,
Accessibility, Integration) must be updated atomically in a single commit.
Since they are all in the same subsystem, the blast radius is contained.

### Migration Path

| Agent | Migration Notes |
|-------|----------------|
| **QA** | Wrap the existing `run_structured_persona` call (which now delegates to `try_structured_or_degrade` internally). Destructure `(result, is_fallback)`. Remove `nonlocal is_fallback` closure — use the returned flag to gate cache writes. Pass `agent_name="QA"`, remove log from `_fallback`. |
| **Security** | Replace inline `try/except` with `try_structured_or_degrade(call=lambda: run_single_shot_review(...), fallback_factory=..., on_success=..., agent_name="Security")`. Remove manual logging. |
| **Accessibility** | Destructure tuple from `run_structured_persona(...)`. Pass `agent_name="Accessibility"`, remove log from `_fallback`. |
| **Integration** | Same as Accessibility. Pass `agent_name="Integration"`. |

### Relationship to Existing Helpers

- **`run_structured_persona`** becomes a thin wrapper around
  `try_structured_or_degrade` that handles Strands Agent construction +
  system-prompt composition. It is not replaced — it gains the degrade
  contract from the new primitive.
- **`run_single_shot_review`** continues to exist unchanged. Security calls
  it inside the `call` callable passed to `try_structured_or_degrade`.
- **No new pattern** is introduced to `LLM_CALLING_PATTERN_DECISION.md`.
  `try_structured_or_degrade` is an internal implementation detail of the
  existing Pattern 2 helper and Security's justified-exception path — not a
  new top-level calling pattern.

---

## 4. Out of Scope (per issue #6936)

- Writing the production implementation
- Modifying any existing call sites
- Migrating blog_writer or investment_team agents (different subsystems,
  more complex retry semantics)
