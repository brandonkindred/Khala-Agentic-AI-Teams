# Extend BaseTeamLead phase sequencing for multi-gate phases

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  

## Goal

Give `BaseTeamLead` a complementary intra-phase gate hook that runs multiple sequential early-exit gates within a single phase, matching devops Phase 4's validation/review shape (quality-gates check → build verifier). No consumer migration in this change.

## Motivation

`_run_gated_phases` is sized for one gate per phase (devops Phase 1–3). Devops Phase 4 does side-effecting validation/review work that accumulates closed-over state, then hits multiple early-return gates inside that same phase. Nesting `_run_gated_phases` would work algorithmically, but a first-class complementary hook makes the intra-phase intent explicit for the later Phase 4 migration without changing the outer phase runner.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Approach | Complementary hook (same contract as `_run_gated_phases`) |
| Method name | `_run_phase_gates` |
| Implementation | Delegate to `_run_gated_phases` — no duplicated loop body |
| Failure contract | `Optional[T]`: gate returns `None` on success, or a failure payload `T`; helper returns that payload on first failure, else `None` |
| Gate signature | `Callable[[], Optional[T]]` (zero-arg; shared state via closures) |
| Empty sequence | Return `None` |
| Exceptions | Propagate — helper does not catch |
| Logging / status | Out of scope inside the helper |
| Consumer migration | Out of scope — no `devops_team/orchestrator.py` changes |
| Nested sequences in `_run_gated_phases` | Rejected — keep the outer runner flat |
| Body + gates helper | Rejected — body stays in the phase callable; gates are the hook's concern |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | Add `_run_phase_gates`; mention it in module/class docs |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for multi-gate success and multi-gate early-exit (plus empty / exception for explicit hook coverage) |

### Files not touched

- `devops_team/orchestrator.py` and its tests
- `backend_code_v2_team/` / `frontend_code_v2_team/` orchestrators
- coding_team orchestration
- `_run_gated_phases` contract or loop body (delegation only from the new hook)

### API

```python
T = TypeVar("T")

class BaseTeamLead:
    def _run_phase_gates(
        self,
        gates: Sequence[Callable[[], Optional[T]]],
    ) -> Optional[T]:
        """Run intra-phase gate callables; return the first failure payload.

        Preconditions: ``gates`` is a sequence (may be empty); each element is
          a zero-arg callable returning ``Optional[T]``.
        Postconditions: same as ``_run_gated_phases`` — first non-``None`` wins;
          all-``None`` / empty → ``None``; exceptions propagate.
        """
        return self._run_gated_phases(gates)
```

Intended consumer shape (future; not in this change):

```python
def _phase4_validation_and_review(...):
    # ... tools, reviews, build quality_gates (closed-over state) ...
    return self._run_phase_gates([
        lambda: failure_if_quality_gates_failed(...),
        lambda: failure_if_build_verify_failed(...),
    ])
```

Outer pipeline continues to use `_run_gated_phases` for Phase 1–3 (and later Phase 5); Phase 4's callable uses `_run_phase_gates` for its internal early-exit gates.

## Testing

In `test_team_lead_base.py`:

1. **Multi-gate success** — two/three gates each return `None`; helper returns `None`; all were invoked in order.
2. **Multi-gate early-exit** — a middle gate returns a failure payload; helper returns that exact object; later gates never run.
3. **Empty sequence** — `[]` → `None`.
4. **Exception propagates** — a raising gate is not caught by the helper.

Coverage floor: 90% on touched files. Verification: `make test` and `make lint` from `backend/`.

## Out of scope

- Migrating devops Phase 4 onto the hook
- Changing `_run_gated_phases` semantics
- Nested sequence support in the outer runner
- Bounded retry/patch-loop changes
- Status/logging inside `_run_phase_gates`
