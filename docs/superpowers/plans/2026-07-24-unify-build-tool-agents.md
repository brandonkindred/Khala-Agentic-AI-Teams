# Unify `_build_tool_agents` Boilerplate Shape Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `BaseV2DevelopmentAgent._assemble_tool_agents` and route both code-v2 teams' module-level `_build_tool_agents` through it without changing roster content.

**Architecture:** Keep deferred imports and module-level `_build_tool_agents(llm)` in each orchestrator. Replace only the final `return {…}` dict literal with a call to a shared static helper that builds `dict(entries)` from `(kind, instance)` varargs. Existing tests keep importing the module-level functions.

**Tech Stack:** Python 3.10+, pytest, Ruff; SE code-v2 orchestrators + `shared/v2_orchestrator.py`.

## Global Constraints

- Do not change either team's tool-agent roster membership or constructor args (`llm` vs no-arg).
- Keep module-level `_build_tool_agents(llm)` in both orchestrators (tests and `run_workflow` depend on it).
- Do not move deferred imports onto the base.
- Never reference GitHub issue numbers in source, comments, or commit messages (PR body may use `Closes #2000`).
- Work in worktree `.worktrees/refactor-2000-unify-build-tool-agents` on branch `refactor/2000-unify-build-tool-agents`.
- Run pytest via `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest` from the worktree `backend/` directory.
- Spec: `docs/superpowers/specs/2026-07-24-unify-build-tool-agents-design.md`.

## File map

| Path | Role |
|---|---|
| `backend/agents/software_engineering_team/shared/v2_orchestrator.py` | Add `_assemble_tool_agents` static helper on `BaseV2DevelopmentAgent` |
| `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` | Route `_build_tool_agents` return through the helper |
| `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` | Route `_build_tool_agents` return through the helper |
| `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py` | Unit tests for `_assemble_tool_agents` |

---

### Task 1: Add `_assemble_tool_agents` (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py` (insert new class after `test_fe_build_tool_agents` / before `test_fe_development_agent_init`)
- Modify: `backend/agents/software_engineering_team/shared/v2_orchestrator.py` (add method after `_build_tool_runners`)

**Interfaces:**
- Consumes: `BaseV2DevelopmentAgent` in `shared/v2_orchestrator.py` (already imports `Dict`, `Tuple`, `Any`)
- Produces: `BaseV2DevelopmentAgent._assemble_tool_agents(*entries: Tuple[Any, Any]) -> Dict[Any, Any]`

- [ ] **Step 1: Write the failing tests**

Insert this class in `test_v2_orchestrator_helpers.py` immediately after `test_fe_build_tool_agents` (before `test_fe_development_agent_init`):

```python
class TestAssembleToolAgents:
    """Unit tests for BaseV2DevelopmentAgent._assemble_tool_agents."""

    def test_empty_returns_empty_dict(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        assert BaseV2DevelopmentAgent._assemble_tool_agents() == {}

    def test_single_entry(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        agent = object()
        out = BaseV2DevelopmentAgent._assemble_tool_agents(("k1", agent))
        assert out == {"k1": agent}

    def test_multiple_entries(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        a, b = object(), object()
        out = BaseV2DevelopmentAgent._assemble_tool_agents(("k1", a), ("k2", b))
        assert out == {"k1": a, "k2": b}

    def test_duplicate_kind_last_wins(self):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        first, second = object(), object()
        out = BaseV2DevelopmentAgent._assemble_tool_agents(("k", first), ("k", second))
        assert out == {"k": second}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestAssembleToolAgents -q
```

Expected: FAIL with `AttributeError: type object 'BaseV2DevelopmentAgent' has no attribute '_assemble_tool_agents'` (or equivalent).

- [ ] **Step 3: Implement the helper**

In `shared/v2_orchestrator.py`, immediately after `_build_tool_runners` (before `_build_progress_callback`), add:

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

Do not change any other method in this file in this task.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestAssembleToolAgents -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents
git add \
  backend/agents/software_engineering_team/shared/v2_orchestrator.py \
  backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py
git commit -m "$(cat <<'EOF'
Add BaseV2DevelopmentAgent._assemble_tool_agents helper.

Shared (kind, instance) roster assembly for code-v2 teams; deferred imports stay
in each orchestrator.
EOF
)"
```

---

### Task 2: Route backend and frontend `_build_tool_agents` through the helper

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` (replace the `return {…}` block inside `_build_tool_agents`)
- Modify: `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` (same)

**Interfaces:**
- Consumes: `BaseV2DevelopmentAgent._assemble_tool_agents(*entries: Tuple[Any, Any]) -> Dict[Any, Any]` from Task 1 (both files already import `BaseV2DevelopmentAgent`)
- Produces: unchanged module-level `_build_tool_agents(llm) -> Dict[ToolAgentKind, Any]` signatures and roster content

- [ ] **Step 1: Update backend `_build_tool_agents` return**

In `backend_code_v2_team/orchestrator.py`, keep the deferred-import docstring and import block unchanged. Replace only the final `return {…}` (currently lines 74–83) with:

```python
    return BaseV2DevelopmentAgent._assemble_tool_agents(
        (ToolAgentKind.DATA_ENGINEERING, DataEngineeringToolAgent(llm)),
        (ToolAgentKind.API_OPENAPI, ApiOpenApiToolAgent(llm)),
        (ToolAgentKind.AUTH, AuthToolAgent(llm)),
        (ToolAgentKind.GIT_BRANCH_MANAGEMENT, GitBranchManagementToolAgent()),
        (ToolAgentKind.BUILD_SPECIALIST, BuildSpecialistAdapterAgent(llm)),
        (ToolAgentKind.TESTING_QA, TestingQAToolAgent(llm)),
        (ToolAgentKind.SECURITY, SecurityToolAgent(llm)),
        (ToolAgentKind.DOCUMENTATION, DocumentationToolAgent(llm)),
    )
```

- [ ] **Step 2: Update frontend `_build_tool_agents` return**

In `frontend_code_v2_team/orchestrator.py`, keep the deferred-import docstring and import block unchanged. Replace only the final `return {…}` (currently lines 88–104) with:

```python
    return BaseV2DevelopmentAgent._assemble_tool_agents(
        (ToolAgentKind.STATE_MANAGEMENT, StateManagementToolAgent()),
        (ToolAgentKind.AUTH, AuthToolAgent()),
        (ToolAgentKind.API_OPENAPI, ApiOpenApiToolAgent()),
        (ToolAgentKind.DOCUMENTATION, DocumentationToolAgent(llm)),
        (ToolAgentKind.TESTING_QA, TestingQAToolAgent(llm)),
        (ToolAgentKind.SECURITY, SecurityToolAgent(llm)),
        (ToolAgentKind.GIT_BRANCH_MANAGEMENT, GitBranchManagementToolAgent()),
        (ToolAgentKind.UI_DESIGN, UiDesignToolAgent(llm)),
        (ToolAgentKind.BRANDING_THEME, BrandingThemeToolAgent(llm)),
        (ToolAgentKind.UX_USABILITY, UxUsabilityToolAgent(llm)),
        (ToolAgentKind.ACCESSIBILITY, AccessibilityToolAgent(llm)),
        (ToolAgentKind.PERFORMANCE, PerformanceToolAgent(llm)),
        (ToolAgentKind.ARCHITECTURE, ArchitectureToolAgent(llm)),
        (ToolAgentKind.BUILD_SPECIALIST, BuildSpecialistAdapterAgent(llm)),
        (ToolAgentKind.LINTER, LinterToolAgent()),
    )
```

- [ ] **Step 3: Run existing roster + helper tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::test_be_build_tool_agents \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::test_fe_build_tool_agents \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestAssembleToolAgents \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py -q
```

Expected: all passed (no failures).

- [ ] **Step 4: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents
git add \
  backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py \
  backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py
git commit -m "$(cat <<'EOF'
Route code-v2 _build_tool_agents through shared assemble helper.

Keeps deferred imports and roster content; only the dict-construction shape
is shared via BaseV2DevelopmentAgent._assemble_tool_agents.
EOF
)"
```

---

### Task 3: Full lint + test closeout

**Files:**
- Verify only (no further production edits expected)

**Interfaces:**
- Consumes: Task 1 helper + Task 2 call sites
- Produces: green `make lint` / `make test` evidence for the PR

- [ ] **Step 1: Run lint**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents/backend
make lint
```

Expected: ruff check + format checks pass with no errors on touched files.

- [ ] **Step 2: Run full test suite**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents/backend
make test
```

Expected: pytest suite passes; 90% coverage floor holds for touched files.

- [ ] **Step 3: Confirm no stray changes**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/refactor-2000-unify-build-tool-agents
git status
```

Expected: clean working tree (or only untracked ignored files). If lint auto-fixed files, commit them:

```bash
git add -u
git commit -m "$(cat <<'EOF'
Apply lint formatting after assemble-helper refactor.
EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| `_assemble_tool_agents(*entries)` on `BaseV2DevelopmentAgent` | Task 1 |
| Backend `_build_tool_agents` uses helper; roster unchanged | Task 2 Step 1 |
| Frontend `_build_tool_agents` uses helper; roster unchanged | Task 2 Step 2 |
| Existing helper/team tests pass unchanged | Task 2 Step 3 |
| New unit tests: empty / one / many / duplicate last-wins | Task 1 |
| `make test` / `make lint` / 90% coverage | Task 3 |
| No roster changes; no deferred-import move; no method override | Global Constraints + Task 2 |

## Placeholder / consistency check

- No TBD/TODO placeholders.
- Signature `_assemble_tool_agents(*entries: Tuple[Any, Any]) -> Dict[Any, Any]` is identical in Task 1 implementation and Task 2 call sites.
- Backend/frontend pair lists match current production roster exactly (including no-arg frontend constructors).
