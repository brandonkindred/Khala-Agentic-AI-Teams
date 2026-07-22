# Add a bounded retry/patch-loop extension point to BaseTeamLead

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  

## Goal

Give `BaseTeamLead` a bounded retry/patch-loop helper that subclasses can use for debug/patch-style iteration patterns, without wiring any consumer yet. This is prerequisite infrastructure for migrating `devops_team`'s debug-patch retry loop onto the shared base.

## Motivation

`devops_team/orchestrator.py`'s debug-patch retry loop retries up to a fixed number of iterations around infra debug/patch agent calls — a bespoke pattern with no analog in the shared team-lead base. A parameterized extension point (max iterations, attempt callable, success-check callable) lets a later migration replace that inline loop without inventing another one-off.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Placement | Instance method only on `BaseTeamLead` (same style as `_report_status`) |
| Abort signal | `attempt` returns `Optional[T]`; `None` aborts the loop |
| Unexpected exceptions | Propagate from `attempt` / `is_success` — callers soft-fail by returning `None` |
| Return shape | `Tuple[bool, Optional[T]]` — `(succeeded, result)` |
| Attempt signature | `attempt(iteration: int) -> Optional[T]` (0-based index) |
| Success check | `is_success(result: T) -> bool` — only invoked when `attempt` returns non-`None` |
| `max_iterations` | Precondition `max_iterations >= 1` (assert) |
| Consumer migration | Out of scope — no `devops_team/orchestrator.py` changes |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | Add `_run_bounded_retry_loop`; update module/class docs |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for success-on-first, success-after-N, exhausted, abort, and precondition |

### Files not touched

- `devops_team/orchestrator.py` and its tests
- `backend_code_v2_team/` / `frontend_code_v2_team/` orchestrators
- Making `DevOpsTeamLeadAgent` subclass `BaseTeamLead`

### API

```python
from typing import Callable, Optional, Tuple, TypeVar

T = TypeVar("T")

class BaseTeamLead:
    def _run_bounded_retry_loop(
        self,
        *,
        max_iterations: int,
        attempt: Callable[[int], Optional[T]],
        is_success: Callable[[T], bool],
    ) -> Tuple[bool, Optional[T]]:
        """Run ``attempt`` up to ``max_iterations`` times until success or abort.

        Preconditions: ``max_iterations >= 1``; ``attempt`` and ``is_success`` are callable.
        Postconditions:
          - On success: returns ``(True, result)`` where ``is_success(result)`` is True.
          - On abort (``attempt`` returns ``None``): returns ``(False, None)`` and does
            not call further iterations.
          - On exhausted retries: returns ``(False, last_non_none_result)``.
          - Exceptions from ``attempt`` / ``is_success`` propagate unchanged.
        """
```

### Loop semantics

1. Assert `max_iterations >= 1`.
2. For `i` in `range(max_iterations)`:
   - `result = attempt(i)`
   - If `result is None`: return `(False, None)`
   - If `is_success(result)`: return `(True, result)`
   - Else keep `last = result` and continue
3. Return `(False, last)` after the loop (``last`` is the final non-`None` attempt result).

Subclass usage (future; not in this change):

```python
succeeded, outcome = self._run_bounded_retry_loop(
    max_iterations=3,
    attempt=lambda i: self._debug_patch_once(i, failures),
    is_success=lambda result: not result.remaining_failures,
)
```

### Error handling

The helper does not catch exceptions. Callers that want soft-fail (devops-style) wrap agent calls inside `attempt` and return `None` on failure.

## Testing

In `test_team_lead_base.py`:

1. **Success on first attempt** — `(True, result)`; `attempt` called once with `0`.
2. **Success after N attempts** — `(True, result)`; `attempt` called N times with `0..N-1`.
3. **Exhausted retries** — `(False, last_result)`; `attempt` called `max_iterations` times.
4. **Abort via `None`** — `(False, None)`; no further iterations after the aborting call.
5. **Precondition** — `max_iterations < 1` raises `AssertionError`.

Coverage floor: 90% on touched files. Verification: `make test` and `make lint` from `backend/`.

## Out of scope

- Migrating the devops debug-patch retry loop onto this helper
- Catching or logging exceptions inside the helper
- A module-level pure-function variant or a named result dataclass
- Changing `BaseTeamLead`'s constructor so devops can subclass it (separate migration work)
