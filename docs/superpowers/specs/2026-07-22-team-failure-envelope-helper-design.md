# Shared team-result failure envelope helper

**Date:** 2026-07-22  
**Status:** Approved for implementation planning  
**Issue:** shared error-result envelope for phase/swarm models (GitHub #2005; parent track #1982)

## Goal

Add a shared “team-result failure envelope” builder in
`software_engineering_team/shared/team_lead_base.py` that both phase-sequential
orchestrators (e.g. devops early returns) and swarm-style mixins can use later
to construct `success=False` + `failure_reason` + optional partial-state results
consistently. This change adds the helper and tests only — no consumers yet.

## Motivation

DevOps early returns repeatedly construct
`DevOpsTeamResult(success=False, failure_reason=..., [completion_package=...])`.
Code-v2 / `BaseTeamLead` often mutates an already-created result’s
`failure_reason` (success already defaults to `False`). Coding-team swarm
mixins will migrate onto the same envelope in a follow-up. Generalizing the
shape now avoids each orchestrator inventing a slightly different failure
constructor.

## Decisions (locked)

| Decision | Choice |
|---|---|
| API surface | Both a factory and a mutator |
| Factory | `build_team_failure_result(result_cls, failure_reason, **partial_state)` |
| Mutator | `apply_team_failure(result, failure_reason, **partial_fields)` → same object |
| Canonical error field | `failure_reason` (matches devops + code-v2) |
| `success` | Always forced to `False`; callers may not override via kwargs |
| Empty `failure_reason` | Allowed (`""`), matching existing model defaults |
| Placement | Module-level helpers alongside `copy_development_result_fields` |
| Consumer migration | Out of scope — no coding_team mixin / devops / code-v2 call-site changes |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | Add `build_team_failure_result` + `apply_team_failure`; brief module-doc mention |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for both helpers |

### Files not touched

- `coding_team_orchestrator.py` / `swarm_*.py` mixins (paired migration)
- `devops_team/orchestrator.py`
- `backend_code_v2_team/` / `frontend_code_v2_team/` orchestrators

### API

```python
def build_team_failure_result(
    result_cls: Callable[..., T],
    failure_reason: str,
    **partial_state: Any,
) -> T:
    """Construct a failure envelope: success=False + failure_reason + optional partial state.

    Preconditions: ``result_cls`` is callable as
      ``result_cls(success=False, failure_reason=..., **partial_state)``;
      ``failure_reason`` is a str; ``partial_state`` must not include ``success``
      or ``failure_reason``.
    Postconditions: returns an instance with ``success is False`` and
      ``failure_reason`` equal to the given string; each ``partial_state`` key is
      forwarded to the constructor.
    """


def apply_team_failure(
    result: Any,
    failure_reason: str,
    **partial_fields: Any,
) -> Any:
    """Mutate an existing result into the failure envelope; return the same object.

    Preconditions: ``result`` is not None and exposes assignable ``success`` /
      ``failure_reason`` attributes (and any keys in ``partial_fields``);
      ``failure_reason`` is a str; ``partial_fields`` must not include ``success``
      or ``failure_reason``.
    Postconditions: ``result.success is False``; ``result.failure_reason`` equals
      the given string; each ``partial_fields`` key is set via ``setattr``;
      returns ``result`` (same identity). Unrelated attributes are left untouched.
    """
```

### Intended future use (not in this change)

```python
# devops-style early return
return build_team_failure_result(
    DevOpsTeamResult,
    f"Cannot create feature branch: {branch_msg}",
)

# code-v2 / BaseTeamLead mutate-in-place
return apply_team_failure(result, f"Setup failed: {exc}")

# devops with partial state
return build_team_failure_result(
    DevOpsTeamResult,
    "Quality gates failed",
    completion_package=package,
)
```

## Testing

In `test_team_lead_base.py`:

1. **Factory builds envelope** — `success is False`, `failure_reason` set.
2. **Factory forwards partial state** — e.g. `completion_package` / `iterations`.
3. **Factory rejects `success` / `failure_reason` in kwargs** — `AssertionError`.
4. **Apply mutates in place** — fields updated; return value is same object.
5. **Apply preserves unrelated fields** — attributes not in the envelope stay unchanged.
6. **Apply rejects `success` / `failure_reason` in kwargs** — `AssertionError`.
7. **Empty `failure_reason` allowed** — `""` is valid for both helpers.

Coverage floor: 90% on touched files. Verification: `make test` and `make lint`
from `backend/`.

## Out of scope

- Migrating `CodingTeamSwarm` mixins onto the helper (paired follow-up).
- Migrating devops early-return gates onto the helper.
- Migrating code-v2 / `BaseTeamLead._run_setup_and_delegate` early returns onto
  `apply_team_failure`.
- Changing result model schemas or renaming fields (`error` vs `failure_reason`)
  on swarm task-graph / `_update` paths.
