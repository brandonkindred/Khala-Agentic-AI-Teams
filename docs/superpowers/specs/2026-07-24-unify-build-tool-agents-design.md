# Unify `_build_tool_agents` Boilerplate Shape

**Status:** Approved 2026-07-24  
**Date:** 2026-07-24  
**Type:** Structural refactor (behavior-preserving)  
**Issue:** #2000  
**Branch / worktree:** `refactor/2000-unify-build-tool-agents` / `.worktrees/refactor-2000-unify-build-tool-agents`

## Problem

`backend_code_v2_team/orchestrator.py` and `frontend_code_v2_team/orchestrator.py` each define a module-level `_build_tool_agents(llm)` that follows the same deferred-import + dict-construction shape. Only the roster content differs (backend includes `DataEngineeringToolAgent`; frontend includes accessibility/architecture/branding/linter/performance/state/UI/UX agents). The surrounding boilerplate is duplicated; the content itself is legitimately team-specific.

## Goals

1. Add a shared assemble/register helper on `BaseV2DevelopmentAgent` for the `(kind, instance) → dict` construction shape.
2. Route both teams' `_build_tool_agents` through that helper after their existing deferred imports.
3. Leave roster membership, constructor args (`llm` vs no-arg), module-level `_build_tool_agents(llm)` call sites, and existing tests unchanged in behavior.

## Non-goals

- Changing either team's tool-agent roster.
- Promoting construction to an overridable method on the base (rejected in favor of a thin assemble helper).
- Moving deferred imports into the base (heavy adapters + frontend circular-import risk).
- Any other `run_workflow` extraction under the parent epic.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Shared API | `BaseV2DevelopmentAgent._assemble_tool_agents(*entries)` | Replaces the shared `return {…}` shape without touching deferred imports |
| Entry form | Varargs of `(kind, instance)` tuples | Matches static-helper style on the base; no intermediate list |
| Module-level functions | Keep `_build_tool_agents(llm)` in each orchestrator | Existing tests and `run_workflow` import/call that name |
| Duplicate kinds | Last-wins (`dict(entries)` semantics) | Same as today's dict literal; no new validation |

## Design

### `BaseV2DevelopmentAgent._assemble_tool_agents`

Static helper in `shared/v2_orchestrator.py`:

```python
@staticmethod
def _assemble_tool_agents(*entries: Tuple[Any, Any]) -> Dict[Any, Any]:
    """Assemble a tool-agent roster from (kind, instance) pairs.

    Preconditions: each entry is a ``(kind, agent)`` pair; kinds are hashable;
      agent instances are already constructed (deferred imports happen in the
      caller). Duplicate kinds are last-wins (same as ``dict(entries)``).
    Postconditions: returns a ``Dict`` mapping each kind to its instance;
      does not import or construct agents itself.
    """
    return dict(entries)
```

### Call sites

Each team's `_build_tool_agents` keeps its deferred-import docstring and import block. The final `return {…}` becomes:

```python
return BaseV2DevelopmentAgent._assemble_tool_agents(
    (ToolAgentKind.DATA_ENGINEERING, DataEngineeringToolAgent(llm)),
    # ... remaining pairs, membership and constructor args unchanged ...
)
```

Frontend follows the same pattern for all current pairs (including no-arg constructors such as `StateManagementToolAgent()` and `LinterToolAgent()`).

`run_workflow` continues to call `_build_tool_agents(self.llm)`.

## Testing

- Existing `test_be_build_tool_agents` / `test_fe_build_tool_agents` and team suite patches of module-level `_build_tool_agents` must pass unchanged.
- Add unit tests for `_assemble_tool_agents` in `test_v2_orchestrator_helpers.py`: empty → `{}`; one entry; multiple entries; duplicate kind last-wins.
- `make test` and `make lint` from `backend/`; 90% coverage floor holds for touched files.

## Success criteria

1. Shared helper exists on `BaseV2DevelopmentAgent`.
2. Both `_build_tool_agents` implementations assemble via that helper; roster content unchanged.
3. Named orchestrator helper/team tests pass unchanged.
4. `make test` / `make lint` pass; coverage floor holds.

## Risk

Low. Mechanical shape change with identical `dict` semantics; failure mode is a mistyped `(kind, instance)` pair — mitigated by existing roster-membership tests.
