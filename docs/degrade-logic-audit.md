# Degrade Logic Audit — SE-Team Gate Agents

**Issue:** #6936  
**Parent Story:** #6931  
**Date:** 2026-08-21  

## Purpose

Audit the 4 existing SE-team gate agents' "try structured output, degrade on
failure" call sites, document semantic differences, and define the canonical
degrade contract. The recommended implementation path is to evolve the
existing `run_structured_persona` helper rather than introduce a new parallel
primitive — see Section 3.

---

## 1. Call-Site Comparison Table

| Dimension | QA Agent | Security Agent | Accessibility Agent | Integration Agent |
|-----------|----------|----------------|---------------------|-------------------|
| **Helper used** | `run_structured_persona` | Direct `try/except` around `run_single_shot_review` | `run_structured_persona` | `run_structured_persona` |
| **Underlying LLM mechanism** | Strands Agent + `structured_output_model` | `generate_structured` (LLMClient API) with `correction_attempts=1` | Strands Agent + `structured_output_model` | Strands Agent + `structured_output_model` |
| **Exception types caught** | All (`Exception`) via helper | All (`Exception`) inline | All (`Exception`) via helper | All (`Exception`) via helper |
| **Logging** | `logger.warning("QA: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Security: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Accessibility: structured_output failed (%s); returning fallback", exc)` | `logger.warning("Integration: structured_output failed (%s); returning failed result", exc)` |
| **Fallback output** | `QAOutput(bugs_found=[], approved=False, quality_gates={"acceptance_evidence":"fail"} if acceptance mode else {}, summary=f"...{exc}")` | `SecurityOutput(vulnerabilities=[], approved=False, summary=f"...{exc}", remediations=[])` | `AccessibilityOutput(issues=[], approved=False, summary=f"...{exc}")` | `IntegrationOutput(passed=False, issues=[], summary=f"...{exc}", fix_task_suggestions=[])` |
| **Retry / correction logic** | Strands implicit: forced tool-choice re-prompt on omitted tool call; validation error fed back for one event-loop correction turn | 1 explicit self-correction attempt inside `generate_structured` (re-prompts with validation error), then exception propagates | Same as QA (Strands implicit correction turns) | Same as QA (Strands implicit correction turns) |
| **Post-success processing** | `_finalize`: re-derives `approved` from severities/gates; mode-dependent logic | Inline: re-derives `approved` via `derive_approved` | `_finalize`: re-derives `approved` from severities | `_finalize`: re-derives `passed` from severities |
| **Cache interaction** | Fallback results are NOT cached (`is_fallback` flag); genuine results cached | Fallback results are NOT returned to cache (early-return before cache-write block) | No caching | No caching |
| **Fail-closed guarantee** | Yes (`approved=False`, plus explicit quality gate fail in acceptance mode) | Yes (`approved=False`) | Yes (`approved=False`) | Yes (`passed=False`) |

---

## 2. Semantic Differences Identified

### Difference A: Underlying LLM Dispatch Mechanism

- **3 agents** (QA, Accessibility, Integration) use the Strands `Agent` with
  `structured_output_model`, which internally uses forced tool-choice to
  extract structured output. Strands does have implicit correction behavior:
  if the model omits the output tool call, Strands sends a follow-up forcing
  prompt; if the tool call has Pydantic-invalid arguments, validation errors
  are fed back for another event-loop turn. These implicit retries happen
  inside the Strands `Agent.__call__` before any exception propagates to
  `run_structured_persona`.
- **Security** uses `run_single_shot_review` → `generate_structured`, which
  supports `correction_attempts=1` (one explicit self-correction retry on
  parse/validation failure before raising).

Both mechanisms provide some form of self-correction before raising, though
the retry budgets and triggering conditions differ (Strands' is implicit
and event-loop-driven; `generate_structured`'s is explicit and bounded).

**Decision:** PRESERVE. Per `LLM_CALLING_PATTERN_DECISION.md`, the Security
agent's migration from Pattern 2 to `run_single_shot_review` is an
already-justified exception — it moved specifically to gain the bounded
corrective retry. The dispatch mechanism difference is preserved at the
call site: the 3 Strands-based agents continue using `run_structured_persona`
(Pattern 2), and Security continues using `run_single_shot_review`. The
degrade contract wraps whichever mechanism the agent uses; it does not
attempt to unify the underlying dispatch.

### Difference B: Logging Message Wording

- QA/Security/Accessibility say "returning fallback"
- Integration says "returning failed result"

**Decision:** UNIFY. Standardize on `"{agent_name}: structured_output failed
({exc}); degrading to safe fallback"` — consistent wording with a
descriptive verb ("degrading"). This can be emitted by the helper itself
when `agent_name` is provided.

### Difference C: Mode-Dependent Fallback Fields (QA Only)

- QA's fallback output varies by mode: in `acceptance_evidence` mode the
  fallback includes `quality_gates={"acceptance_evidence": "fail"}` to
  ensure downstream consumers that key off gate values (rather than
  `approved`) still see the blocking signal.
- Other agents have uniform fallback shapes.

**Decision:** PRESERVE site-specific behavior. The `fallback_factory`
callable pattern already handles this cleanly — each agent supplies its own
factory that knows the shape/mode. The helper does not need to understand
modes.

### Difference D: Post-Success Processing (`on_success` / `_finalize`)

- All 4 agents re-derive their pass/fail flag from reported findings (not
  trusting the LLM's self-reported `approved`/`passed` value).
- Logic is identical in concept but differs in field names (`approved` vs
  `passed`) and in QA's mode-conditional gate logic.

**Decision:** PRESERVE site-specific behavior via the `on_success` callback.
The helper calls `on_success(result)` on the happy path and skips it for
fallback results (fallback is already final/safe).

### Difference E: Self-Correction Retry Budget

- Security's underlying `generate_structured` gets one explicit
  self-correction attempt (the LLM is re-prompted with the validation
  error) before raising.
- The Strands mechanism used by the other 3 has implicit correction turns
  (forced tool-choice re-prompt on omission; validation error feedback on
  invalid arguments) that happen inside the agent's event loop before the
  exception propagates out.

Both approaches provide correction — the difference is in explicitness and
budget visibility. From the degrade helper's perspective, these are
implementation details of the `call` that happen before any exception
reaches the catch boundary.

**Decision:** PRESERVE. This is an implementation detail of the underlying
call mechanism, not of the degrade contract. The helper simply wraps
whatever call the agent makes. Correction/retry logic (implicit Strands
turns or explicit `correction_attempts`) happens *inside* the try-block,
before the catch.

### Difference F: Cache Bypass on Fallback

- QA and Security avoid caching fallback results (so a subsequent call
  retries the LLM instead of serving a stale failure).
- Accessibility and Integration have no caching at all.

**Decision:** PRESERVE as caller responsibility. Caching is orthogonal to
the degrade contract. The helper should return a signal (boolean) so
callers can decide whether to cache. The existing `is_fallback` flag
pattern in QA is the cleanest approach.

---

## 3. Canonical Degrade Contract

### Approach: Evolve `run_structured_persona`, Not a Parallel Helper

Per `LLM_CALLING_PATTERN_DECISION.md`, Pattern 2 (`run_structured_persona`)
is the justified pattern for these 4 gate agents. Rather than introducing
a new `try_structured_or_degrade` primitive alongside it — which would
create a parallel helper tree and conflict with the decision record — the
recommended path is to evolve `run_structured_persona` itself with two
additions:

1. **Return `(result, is_fallback)` tuple** instead of bare `result`.
2. **Emit a standardized log line** on fallback (centralized, rather than
   relying on each `fallback_factory` to log its own warning).

For the Security agent (which uses `run_single_shot_review`, a justified
Pattern exception), the degrade contract is already implemented inline.
If the `is_fallback` signal or standardized logging is required there too,
a thin wrapper or a shared utility function can be added without creating
a full parallel helper.

### Evolved Signature

```python
def run_structured_persona(
    *,
    model: Any,
    system_prompt: str,
    user_prompt: str,
    output_model: type[OutputT],
    fallback_factory: Callable[[Exception], OutputT],
    agent_factory: Callable[..., Any],
    on_success: Callable[[OutputT], OutputT] | None = None,
    system_prompt_content: List[Any] | None = None,
    agent_name: str = "",
) -> tuple[OutputT, bool]:
    """Run a one-shot structured-output Strands Agent call with a safe fallback.

    Returns
    -------
    tuple[OutputT, bool]
        ``(result, is_fallback)`` — the output instance and whether it came
        from the fallback path. Callers use ``is_fallback`` to decide
        whether to cache the result, emit metrics, etc.

    Additions over current implementation:
    - Returns a tuple with ``is_fallback`` flag (backward-incompatible
      signature change — callers must be updated atomically).
    - When ``agent_name`` is provided, emits a standardized
      ``logger.warning`` on fallback:
      "{agent_name}: structured_output failed ({exc}); degrading to safe
      fallback"

    All other behavior is unchanged from the current implementation:
    - Builds composed system prompt via build_system_prompt_with_content
    - Creates a fresh Strands Agent via agent_factory
    - Calls agent with structured_output_model=output_model
    - On success: optionally runs on_success(result)
    - On failure: returns fallback_factory(exc)
    - Does not retry (Strands' implicit correction turns happen inside
      the agent call, before any exception reaches this catch boundary)
    - Catches broad Exception (not BaseException)
    - Exceptions from fallback_factory propagate (programming error)
    """
```

### Behavior Contract

| Step | Happy Path | Failure Path |
|------|-----------|--------------|
| 1. Build composed system prompt | Succeeds | Raises → step 4 |
| 2. Create agent, invoke with `structured_output_model` | Returns result | Raises → step 4 |
| 3. Validate result type, apply `on_success` | Returns `OutputT` | Raises → step 4 |
| 4. Log warning (if `agent_name` given) | — | `logger.warning(...)` |
| 5. Return | `(result, False)` | `(fallback_factory(exc), True)` |

### Migration Path for Each Agent

| Agent | Migration Notes |
|-------|----------------|
| **QA** | Update call site to destructure `(result, is_fallback) = run_structured_persona(...)`. Remove the `nonlocal is_fallback` closure pattern — use the returned flag directly to gate cache writes. Pass `agent_name="QA"` and remove the log line from `_fallback`. |
| **Security** | No change to `run_single_shot_review` usage. The inline `try/except` stays (justified Pattern exception per decision doc). Optionally standardize the log line wording to match the canonical format. |
| **Accessibility** | Update call site to destructure tuple. Pass `agent_name="Accessibility"` and remove the log line from `_fallback`. |
| **Integration** | Same as Accessibility. Pass `agent_name="Integration"`. |

### Backward Compatibility Note

The signature change from `-> OutputT` to `-> tuple[OutputT, bool]` is
breaking. All 3 callers (QA, Accessibility, Integration) must be updated
atomically in one commit. Since there are only 3 call sites and they are
all in the same subsystem, this is low-risk.

### Relationship to `LLM_CALLING_PATTERN_DECISION.md`

This audit and its recommendations are consistent with the decision record:

- Pattern 2 (`run_structured_persona`) remains the pattern for the 3
  Strands-based persona agents. We evolve it in place.
- Security's `run_single_shot_review` usage remains a justified exception
  (already documented in the decision record as a migration to gain bounded
  corrective retry). We do not attempt to force it onto Pattern 2.
- No new pattern or parallel helper tree is introduced.

---

## 4. Out of Scope (per issue #6936)

- Writing the production implementation of the evolved signature
- Modifying any existing call sites
- Migrating blog_writer or investment_team agents (different subsystems,
  more complex retry semantics)
- Migrating Security onto `run_structured_persona` (justified exception per
  decision record)
