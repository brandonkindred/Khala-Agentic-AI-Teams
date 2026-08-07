# Strategy Lab Orchestrator Ownership Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an AST ownership regression test so moved Strategy Lab orchestrator helpers cannot regain top-level function bodies in `api/main.py`, while keeping existing identity/deferred checks and documenting the guard.

**Architecture:** Extend `test_orchestrator_api.py` with `_MOVED_CALLABLES` and a top-level `ast` walk of `api.main` source. Aliases remain allowed; only `FunctionDef` / `AsyncFunctionDef` names intersecting the moved-callable set fail. Finalize and other deferred symbols stay out of the guard.

**Tech Stack:** Python 3.10+, pytest, `ast`, `inspect`

**Spec:** `docs/superpowers/specs/2026-08-07-strategy-lab-orchestrator-ownership-regression-design.md`

**Worktree:** `.worktrees/issue-5516-orchestrator-regression-coverage` (or equivalent off current `main`)

## Global Constraints

- No intentional runtime behavior change (tests + docs only)
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only: `Closes #N`)
- Design-by-Contract docstrings on new test helpers/tests
- Ruff line-length 120; Python 3.10+
- Do **not** extract finalize / deferred Temporal bodies
- Do **not** add new behavioral smokes beyond ownership/identity
- Ownership walk is **top-level module body only** (no nested defs)
- `_MOVED_CALLABLES` excludes non-callables (`STRATEGY_LAB_TERMINAL_STATUSES`)

## File map

| File | Role |
|---|---|
| `backend/agents/investment_team/tests/test_orchestrator_api.py` | Identity tests + new AST ownership guard |
| `backend/agents/investment_team/strategy_lab/ORCHESTRATOR_API_BOUNDARIES.md` | One-sentence pointer to the regression test |

---

### Task 1: AST ownership guard in `test_orchestrator_api.py`

**Files:**
- Modify: `backend/agents/investment_team/tests/test_orchestrator_api.py`

**Interfaces:**
- Consumes: existing `_MOVED` / `_DEFERRED` tuples; `investment_team.api.main`
- Produces: `_MOVED_CALLABLES`; `test_api_main_has_no_moved_helper_function_bodies`

- [ ] **Step 1: Update module docstring**

Replace the module docstring with:

```python
"""Tests for ``strategy_lab.orchestrator_api`` ownership and façade identity.

Preconditions:
    ``investment_team.api.main`` is importable.
Postconditions:
    Moved symbols are defined on ``orchestrator_api`` (not only via ``__getattr__``)
    and match ``api.main`` aliases. ``api.main`` has no top-level function bodies for
    moved callables (assignment aliases only). Deferred Temporal-hot symbols still
    resolve via ``__getattr__`` to ``api.main`` until a later extract moves them.
"""
```

- [ ] **Step 2: Add imports and `_MOVED_CALLABLES`**

After the existing imports, ensure:

```python
from __future__ import annotations

import ast
import inspect

import pytest

from investment_team.api import main as api_main
from investment_team.strategy_lab import orchestrator_api
```

Keep `_MOVED` and `_DEFERRED` as they are today. Immediately after `_MOVED`, add:

```python
_MOVED_CALLABLES = tuple(
    name for name in _MOVED if name != "STRATEGY_LAB_TERMINAL_STATUSES"
)
```

- [ ] **Step 3: Add the ownership test**

Append (near the other tests):

```python
def test_api_main_has_no_moved_helper_function_bodies() -> None:
    """Moved orchestrator callables must not regain ``def`` bodies in ``api.main``.

    Preconditions:
        ``api_main`` is the loaded ``investment_team.api.main`` module.
    Postconditions:
        No top-level ``FunctionDef`` / ``AsyncFunctionDef`` in ``api.main``'s
        source is named in ``_MOVED_CALLABLES``. Assignment aliases remain allowed.
    """
    source = inspect.getsource(api_main)
    tree = ast.parse(source)
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    leaked = sorted(defined & set(_MOVED_CALLABLES))
    assert leaked == [], (
        "Moved Strategy Lab orchestrator helpers must not have function bodies "
        f"in api.main; found def(s): {leaked}"
    )
```

- [ ] **Step 4: Run GREEN on current tree**

```bash
cd backend
PYTHONPATH=agents pytest agents/investment_team/tests/test_orchestrator_api.py -q
```

Expected: PASS (all tests, including the new ownership guard).

- [ ] **Step 5: Negative sanity (do not commit)**

Temporarily insert at the bottom of `api/main.py` (then revert before commit):

```python
def _persist_run_state(*args, **kwargs):  # ownership-guard probe — revert
    raise RuntimeError("probe")
```

Re-run the ownership test only:

```bash
PYTHONPATH=agents pytest agents/investment_team/tests/test_orchestrator_api.py::test_api_main_has_no_moved_helper_function_bodies -q
```

Expected: FAIL mentioning `_persist_run_state`.

Revert the probe (`git checkout -- backend/agents/investment_team/api/main.py`). Re-run full `test_orchestrator_api.py` → PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/agents/investment_team/tests/test_orchestrator_api.py
git commit -m "$(cat <<'EOF'
Add AST ownership guard for Strategy Lab orchestrator helpers.

EOF
)"
```

---

### Task 2: Boundaries doc note

**Files:**
- Modify: `backend/agents/investment_team/strategy_lab/ORCHESTRATOR_API_BOUNDARIES.md`

**Interfaces:**
- Consumes: partial body move section
- Produces: one-sentence pointer to the regression test

- [ ] **Step 1: Add note under partial body move**

In the section titled like `## Partial body move (persist / reconcile / purge / dispatch)` (or equivalent after cluster-2 landed), after the bullet list of bodies / deferred names, add:

```markdown
**Ownership regression:** `tests/test_orchestrator_api.py` asserts moved callables
have no top-level function bodies in `api.main` (aliases only). Extend `_MOVED` /
`_MOVED_CALLABLES` when a later extract lands more bodies.
```

Do not mention GitHub issue numbers.

- [ ] **Step 2: Commit**

```bash
git add backend/agents/investment_team/strategy_lab/ORCHESTRATOR_API_BOUNDARIES.md
git commit -m "$(cat <<'EOF'
Document Strategy Lab orchestrator ownership regression test.

EOF
)"
```

---

### Task 3: Smoke, lint, PR

**Files:** none new (verification)

- [ ] **Step 1: Ruff**

```bash
cd backend
ruff check agents/investment_team/tests/test_orchestrator_api.py
ruff format --check agents/investment_team/tests/test_orchestrator_api.py
```

Fix if needed; commit format-only if required.

- [ ] **Step 2: Smoke**

```bash
cd backend
PYTHONPATH=agents pytest \
  agents/investment_team/tests/test_orchestrator_api.py \
  agents/investment_team/tests/test_strategy_lab_routes.py \
  -q --tb=line
```

Expected: PASS.

- [ ] **Step 3: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "Add Strategy Lab orchestrator ownership regression guard" --body "$(cat <<'EOF'
## Summary
- AST test fails if moved Strategy Lab orchestrator helpers regain function bodies in `api/main.py`
- Keep identity/deferred façade checks; document the guard in `ORCHESTRATOR_API_BOUNDARIES.md`
- Finalize and other deferred Temporal helpers remain out of the ownership set until extracted

## Test plan
- [ ] `pytest` `test_orchestrator_api.py` (including ownership guard)
- [ ] `pytest` `test_strategy_lab_routes.py` smoke
- [ ] Optional: temporary `def _persist_run_state` probe in `api.main` fails the guard (reverted)

Closes #5516
EOF
)"
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|---|---|
| AST ownership guard, top-level only | Task 1 |
| `_MOVED_CALLABLES` excludes constants | Task 1 |
| Keep identity/deferred tests | Task 1 |
| Boundaries note | Task 2 |
| No finalize extract / no new behavioral smokes | Global constraints |
| Route smoke + PR | Task 3 |

No placeholders; negative probe is explicitly revert-before-commit.
