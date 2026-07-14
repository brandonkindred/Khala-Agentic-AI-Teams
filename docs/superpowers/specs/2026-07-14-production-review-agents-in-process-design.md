# Production Review Agents — Force In-Process Code Review

**Status:** Approved 2026-07-14  
**Date:** 2026-07-14  
**Type:** Bug fix on landed #1273 wiring  
**Issue:** [Closes #1273](https://github.com/brandonkindred/Khala-Agentic-AI-Teams/issues/1273)

## Problem

[#1273](https://github.com/brandonkindred/Khala-Agentic-AI-Teams/issues/1273) asked production callers (`temporal/activities.py`, `api/background.py`) to pass real `code_review_agent` / `build_verifier` / `linting_tool_agent` into `run_workflow`, with a degrade-to-`{}` fallback and a Temporal nested-workflow spike.

That wiring already landed on `main` (drive-by in PR #1341) via `shared/production_review_agents.py`, but the in-process guard is ineffective:

- `build_production_review_kwargs_in_process` sets `TEMPORAL_ADDRESS=disabled` only during `CodeReviewAgent()` construction, then restores it.
- Temporal dispatch is decided at **`run()`** time via `_code_review_temporal_enabled()`, which re-reads the environment.
- `CodeReviewAgent.__init__` does not capture temporal mode.

So Temporal activity callers still risk nested-workflow deadlock when `CodeReviewAgent.run` dispatches a durable `CodeReviewWorkflow` from inside an already-running activity. Issue #1273 remains open because PR #1341 only closed #1287.

## Goals

1. Force in-process code review for agents constructed for Temporal activity callers, for the lifetime of that instance.
2. Keep thread-mode callers (`api/background.py`) free to use Temporal dispatch.
3. Preserve degrade-to-`{}` on construction failure.
4. Close #1273 with a focused PR that proves the guard works at `run()` time.

## Non-goals

- Changing review quality, chunking, false-positive filtering, or prompt content (#1274–#1275).
- Wiring QA/security differently (already covered by `_build_tool_agents`).
- Observability / circuit-breaker work (#1276–#1277).
- Redesigning the production helper API beyond fixing the in-process path.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Force mechanism | `CodeReviewAgent(..., force_in_process: bool = False)` | Explicit, instance-scoped, no global env mutation |
| Rejected: env dance | Temporary `TEMPORAL_ADDRESS=disabled` | Only affects construction; `run()` re-reads env |
| Rejected: coordinator wrapper | Thin adapter around `run_coordinator` | Duplicates dispatch; leaves other nested callers unprotected |
| Rejected: contextvar / thread-local | Override `_code_review_temporal_enabled` | Implicit; easy to leak across threads |
| Thread-mode helper | Unchanged | Background threads may still benefit from durable Temporal review |
| Default | `force_in_process=False` | Preserves current default Temporal-first behavior |

## Architecture

```
api/background.py
  └─ build_production_review_kwargs()
       └─ CodeReviewAgent(get_client(...))          # Temporal OK

temporal/activities.py
  └─ build_production_review_kwargs_in_process()
       └─ CodeReviewAgent(..., force_in_process=True)
            └─ run() → always run_coordinator(...)  # never _run_via_temporal
```

### `CodeReviewAgent`

- Add `force_in_process: bool = False` to `__init__`; store as `self._force_in_process`.
- In `run()`, skip the Temporal branch when `self._force_in_process` is true (check before `_code_review_temporal_enabled()`).
- Document the contract: when forced, `run` never starts a Temporal worker or child workflow.

### `build_production_review_kwargs_in_process`

- Construct `CodeReviewAgent(get_client("code_review"), force_in_process=True)`.
- Remove all `TEMPORAL_ADDRESS` save/restore logic.
- Keep try/except → `{}` degrade path and the other two kwargs (`build_verifier`, `linting_tool_agent`).

### Call sites

No signature changes at `activities.py` / `background.py` — they already splat the helpers.

## Error handling

Unchanged: any construction failure in either helper logs a warning and returns `{}`, so `run_workflow` keeps today's `None` fallbacks (free-text LLM review / stub build / skip lint). The force flag must not make construction more likely to fail.

## Testing

1. **Unit — agent:** With `TEMPORAL_ADDRESS` set to a real address and `_code_review_temporal_enabled` patched to `True`, `CodeReviewAgent(..., force_in_process=True).run(...)` must call `run_coordinator` and must **not** call `_run_via_temporal` / start a worker.
2. **Unit — agent control:** Same setup with `force_in_process=False` still takes the Temporal path (existing temporal tests remain authoritative).
3. **Unit — helper:** `build_production_review_kwargs_in_process` constructs with `force_in_process=True`; remove tests that assert env mutation; keep degrade-to-`{}` and env-restoration-is-unnecessary coverage replaced by “does not mutate `TEMPORAL_ADDRESS`”.
4. **Call-site tests:** Existing four call-site assertions in `test_production_review_agents.py` stay; no change required unless helper patching breaks.
5. **`_validate_findings`:** Already covered in the same test module; no new requirement beyond keeping those tests green.

## Verification / close-out

- Targeted pytest for agent force flag + production helper.
- PR body includes `Closes #1273`.
- Confirm no remaining `TEMPORAL_ADDRESS` mutation in `production_review_agents.py`.
