# Degrade Logic Audit — SE-Team Gate Agents

**Issue:** #6936  
**Parent Story:** #6931  
**Date:** 2026-08-21  

## Purpose

Audit the 4 existing SE-team gate agents' "try structured output, degrade on
failure" call sites, document semantic differences, and define the canonical
contract that a unified `try_structured_or_degrade` helper must satisfy.

---

## 1. Call-Site Comparison Table

| Dimension | QA Agent | Security Agent | Accessibility Agent | Integration Agent |
|-----------|----------|----------------|---------------------|-------------------|
| **Helper used** | `run_structured_persona` | Direct `try/except` around `run_single_shot_review` | `run_structured_persona` | `run_structured_persona` |
| **Underlying LLM mechanism** | Strands Agent + `structured_output_model` | `generate_structured` (LLMClient API) with `correction_attempts=1` | Strands Agent + `structured_output_model` | Strands Agent + `structured_output_model` |
| **Exception types caught** | All (`Exception`) via helper | All (`Exception`) inline | All (`Exception`) via helper | All (`Exception`) via helper |
| **Logging** | `logger.warning("QA: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Security: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Accessibility: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Integration: structured_output failed (%s); returning failed result", exc)` |
| **Fallback output** | `QAOutput(bugs_found=[], approved=False, quality_gates={"acceptance_evidence":"fail"} if acceptance mode else {}, summary=f"...{exc}")` | `SecurityOutput(vulnerabilities=[], approved=False, summary=f"...{exc}", remediations=[])` | `AccessibilityOutput(issues=[], approved=False, summary=f"...{exc}")` | `IntegrationOutput(passed=False, issues=[], summary=f"...{exc}", fix_task_suggestions=[])` |
| **Retry logic** | None (single attempt) | 1 self-correction attempt inside `generate_structured`, then exception propagates | None (single attempt) | None (single attempt) |
| **Post-success processing** | `_finalize`: re-derives `approved` from severities/gates; mode-dependent logic | Inline: re-derives `approved` via `derive_approved` | `_finalize`: re-derives `approved` from severities | `_finalize`: re-derives `passed` from severities |
| **Cache interaction** | Fallback results are NOT cached (`is_fallback` flag); genuine results cached | Fallback results are NOT returned to cache (early-return before cache-write block) | No caching | No caching |
| **Fail-closed guarantee** | Yes (`approved=False`, plus explicit quality gate fail in acceptance mode) | Yes (`approved=False`) | Yes (`approved=False`) | Yes (`passed=False`) |

---

## 2. Semantic Differences Identified

### Difference A: Underlying LLM Dispatch Mechanism

- **3 agents** (QA, Accessibility, Integration) use the Strands `Agent` with
  `structured_output_model`, which internally uses forced tool-choice to
  extract structured output. No self-correction retry exists at this layer —
  if the model returns garbage, the single attempt fails immediately.
- **Security** uses `run_single_shot_review` → `generate_structured`, which
  supports `correction_attempts=1` (one self-correction retry on
  parse/validation failure before raising).

**Decision:** UNIFY. The canonical helper should support an optional
`correction_attempts` parameter (default 0 for current Strands-based
behavior, configurable for agents like Security that benefit from a retry).
Alternatively, since the Strands `structured_output_model` mechanism doesn't
support correction attempts, the helper could offer two backends:
`"strands"` and `"llm_client"`. However, since the parent story (#6931)
names the helper `try_structured_or_degrade`, and the majority pattern is
Strands-based, the simplest unification is to keep the Strands backend as
default and allow Security to continue using `run_single_shot_review`
internally while wrapping it in the shared degrade contract. See proposed
signature below.

### Difference B: Logging Message Wording

- QA/Security/Accessibility say "returning fallback"
- Integration says "returning failed result"

**Decision:** UNIFY. Standardize on `"{agent_name}: structured_output failed
({exc}); degrading to safe fallback"` — consistent wording with a
descriptive verb ("degrading") that matches the helper's name.

### Difference C: Mode-Dependent Fallback Fields (QA Only)

- QA's fallback output varies by mode: in `acceptance_evidence` mode the
  fallback includes `quality_gates={"acceptance_evidence": "fail"}` to
  ensure downstream consumers that key off gate values (rather than
  `approved`) still see the blocking signal.
- Other agents have uniform fallback shapes.

**Decision:** PRESERVE site-specific behavior. The `fallback_factory`
callable pattern already handles this cleanly — each agent supplies its own
factory that knows the shape/mode. The canonical helper does not need to
understand modes.

### Difference D: Post-Success Processing (`on_success` / `_finalize`)

- All 4 agents re-derive their pass/fail flag from reported findings (not
  trusting the LLM's self-reported `approved`/`passed` value).
- Logic is identical in concept but differs in field names (`approved` vs
  `passed`) and in QA's mode-conditional gate logic.

**Decision:** PRESERVE site-specific behavior via the `on_success` callback.
The canonical helper calls `on_success(result)` on the happy path and skips
it for fallback results (fallback is already final/safe).

### Difference E: Self-Correction Retry

- Security's underlying `generate_structured` gets one free self-correction
  attempt (the LLM is re-prompted with the validation error) before raising.
- The Strands mechanism used by the other 3 has no such retry.

**Decision:** PRESERVE. This is an implementation detail of the underlying
call mechanism, not of the degrade contract. The helper doesn't need to
unify retry behavior — it simply wraps whatever call the agent makes. The
retry (or lack thereof) happens *inside* the try-block, before the catch.

### Difference F: Cache Bypass on Fallback

- QA and Security avoid caching fallback results (so a subsequent call
  retries the LLM instead of serving a stale failure).
- Accessibility and Integration have no caching at all.

**Decision:** PRESERVE as caller responsibility. Caching is orthogonal to
the degrade contract. The helper should return a signal (boolean or the
result itself is distinguishable) so callers can decide whether to cache.
The existing `is_fallback` flag pattern in QA is the cleanest approach.

---

## 3. Canonical Helper Contract

### Proposed Signature

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
        its Strands Agent invocation, ``run_single_shot_review`` call, or
        any other structured-output mechanism.
    fallback_factory
        Called with the caught exception; must return a safe, final output
        instance (e.g. approved=False, empty findings). Must not raise.
    on_success
        Optional post-processing hook applied to a successful result before
        returning. Skipped for fallback results (those are already final).
        If ``on_success`` itself raises, the exception is caught and routed
        to ``fallback_factory``.
    agent_name
        Human-readable agent label for the log message (e.g. "QA",
        "Security").
    logger
        Logger instance. If provided, emits a ``warning`` on fallback.

    Returns
    -------
    tuple[OutputT, bool]
        ``(result, is_fallback)`` — the output instance and whether it came
        from the fallback path. Callers use ``is_fallback`` to decide
        whether to cache the result, emit metrics, etc.

    Behavior Contract
    -----------------
    1. Calls ``call()``.
    2. On success, validates the result type if applicable, then returns
       ``(on_success(result), False)`` (or ``(result, False)`` if
       ``on_success`` is None).
    3. On any ``Exception`` from ``call()`` OR from ``on_success()``:
       a. Logs at WARNING: "{agent_name}: structured_output failed ({exc});
          degrading to safe fallback"
       b. Returns ``(fallback_factory(exc), True)``.
    4. Exceptions from ``fallback_factory`` propagate — a broken fallback is
       a programming error and must not be silently swallowed.
    5. The helper NEVER retries the call. Retry logic (correction attempts,
       parse-retry loops) belongs inside ``call()`` — the helper only sees
       the final success-or-failure outcome.
    6. The helper catches broad ``Exception`` (not ``BaseException``) —
       ``KeyboardInterrupt``, ``SystemExit``, etc. propagate.
    """
```

### Behavior Contract Summary

| Step | Happy Path | Failure Path |
|------|-----------|--------------|
| 1. Invoke `call()` | Returns `OutputT` | Raises `Exception` |
| 2. Type-check result | Passes | (caught by step 4) |
| 3. Apply `on_success` | Returns `OutputT` | Raises → treated as failure |
| 4. Log warning | — | `logger.warning(...)` |
| 5. Return | `(result, False)` | `(fallback_factory(exc), True)` |

### Migration Path for Each Agent

| Agent | Migration Notes |
|-------|----------------|
| **QA** | Wrap existing `run_structured_persona(...)` invocation inside `call=lambda: run_structured_persona(...)`. Use returned `is_fallback` to gate cache writes (replacing the `nonlocal is_fallback` flag). |
| **Security** | Wrap `run_single_shot_review(...)` call inside `call`. Remove inline `try/except`. Move the post-success `derive_approved` logic into `on_success`. |
| **Accessibility** | Wrap `run_structured_persona(...)` inside `call`. Straightforward 1:1 migration. |
| **Integration** | Same as Accessibility — straightforward. |

### Why a Tuple Return Instead of a Sentinel

Returning `(result, is_fallback)` rather than embedding a `.is_fallback`
attribute on the result:
- Keeps output models clean (no framework concerns leaking into domain types).
- The caller already needs to branch on fallback status (for caching, metrics);
  a boolean in the return is explicit and hard to ignore.
- Compatible with both Pydantic models and plain dicts.

### Relationship to `run_structured_persona`

`try_structured_or_degrade` is a *lower-level* contract than
`run_structured_persona`. In the long term, `run_structured_persona` can be
reimplemented *in terms of* `try_structured_or_degrade`:

```python
def run_structured_persona(...) -> OutputT:
    def _call() -> OutputT:
        composed_prompt = build_system_prompt_with_content(...)
        agent = agent_factory(model=model, system_prompt=composed_prompt)
        agent_result = agent(user_prompt, structured_output_model=output_model)
        result = agent_result.structured_output
        if not isinstance(result, output_model):
            raise TypeError(...)
        return result

    result, _ = try_structured_or_degrade(
        call=_call,
        fallback_factory=fallback_factory,
        on_success=on_success,
    )
    return result
```

This preserves backward compatibility while unifying the degrade contract.

---

## 4. Out of Scope (per issue #6936)

- Writing the production implementation of `try_structured_or_degrade`
- Modifying any existing call sites
- Migrating blog_writer or investment_team agents (different subsystems,
  more complex retry semantics)
