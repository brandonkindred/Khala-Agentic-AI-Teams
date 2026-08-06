# Strategy Lab AST-helper Duplication Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the open question on AST-traversal duplication across Strategy Lab `quality_gates` and `coverage_probe` by documenting an audit matrix in the PR body and consolidating every *identical* / *unifiable-without-semantic-change* primitive into `strategy_lab/ast_utils/`.

**Architecture:** One-way dependency: `quality_gates` and `coverage_probe` may import `ast_utils`; `ast_utils` must not import either. Domain gate/probe logic stays put. Shared helpers are pure `ast` functions. Call sites keep existing private names via thin wrappers or re-exports so import churn stays local.

**Tech Stack:** Python 3.10+, `ast`, pytest, existing investment_team Strategy Lab tests

## Global Constraints

- Worktree: `.worktrees/issue-3051-ast-helper-audit` on branch `issue-3051-ast-helper-audit`
- In-scope inventory files (must appear in the audit matrix):
  - `backend/agents/investment_team/strategy_lab/quality_gates/code_safety.py`
  - `backend/agents/investment_team/strategy_lab/quality_gates/code_safety_ast.py`
  - `backend/agents/investment_team/strategy_lab/quality_gates/code_conformance/ast_helpers.py`
  - `backend/agents/investment_team/strategy_lab/coverage_probe/predicate_resolution.py`
  - `backend/agents/investment_team/strategy_lab/coverage_probe/subcond_builder.py`
- Bonus call-site migrations allowed (same primitives, not required inventory rows): `coverage_probe/runtime_instrument.py`, `quality_gates/universe_injection.py`
- Out of scope: realism gates; changing coverage-probe branch-coverage *semantics* or the CI check itself
- Unification bar: extract only when both call sites already agree on edge-case behavior, or extract a strict common core with thin local wrappers that preserve each caller's contract
- No gate pass/fail or probe report semantic changes — existing tests are the oracle
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body / issue comment only — use `Closes #3051` in the PR body)
- No long-lived audit doc in-repo; the audit matrix lives in the PR body (and a short issue comment)
- Design-by-Contract docstrings (Preconditions / Postconditions) on every new public `ast_utils` function
- Coverage: new/changed code ≥ 90% line coverage
- If `subcond_builder.py` or `subcondition_visitor.py` change, re-run the dedicated branch-coverage check from `strategy_lab/README.md`

## File map

| File | Role |
|---|---|
| `strategy_lab/ast_utils/__init__.py` | Public re-exports of shared primitives |
| `strategy_lab/ast_utils/names.py` | `name_or_attr`, call-name helpers |
| `strategy_lab/ast_utils/strategy_shape.py` | Strategy-subclass + `on_bar` discovery helpers |
| `strategy_lab/ast_utils/scopes.py` | Method-body iteration without nested-def descent (only if Task 1 marks it shared / worth centralizing) |
| `quality_gates/code_safety_ast.py` | Become thin wrappers / import from `ast_utils` for migrated symbols |
| `quality_gates/code_conformance/ast_helpers.py` | Import shared primitives from `ast_utils` instead of `code_safety_ast` where migrated |
| `coverage_probe/subcond_builder.py` | `_func_name` delegates to shared core |
| `coverage_probe/runtime_instrument.py` | Bonus: `_is_strategy_subclass` / `_find_on_bar` use shared helpers |
| `quality_gates/universe_injection.py` | Bonus: `_find_on_bar_methods` uses shared helper |
| `investment_team/tests/test_strategy_lab_ast_utils.py` | Unit tests for `ast_utils` |
| PR body | Full audit matrix + decisions (acceptance artifact) |

## Preliminary candidates (Task 1 must confirm or reject)

| Candidate | Sites | Likely class | Notes |
|---|---|---|---|
| Strategy-base / subclass check | `code_safety_ast._find_strategy_subclasses`, `runtime_instrument._is_strategy_subclass` | `identical` / `unifiable` | Same `Strategy` / `contract.Strategy` shape |
| Find `on_bar` on a `ClassDef` | `code_safety_ast._find_on_bar_method`, `universe_injection._find_on_bar_methods` | `unifiable` | List vs first-only wrapper |
| Name/attr extraction | `code_safety_ast._get_call_name`, `subcond_builder._func_name` | `unifiable` via common core | Callers differ: `""` vs `None`, case folding |
| Tree-wide `_find_on_bar` with entry/signal fallbacks | `predicate_resolution._find_on_bar` | `superficial` vs class-scoped finders | Different search space + fallbacks |
| `_bar_param_name` vs `_bar_parameter_name` | probe vs universe_injection | `superficial` unless core-only | Fallback `"bar"` vs fail-closed `None` |
| Position pin / strip / classify | `code_safety` vs `predicate_resolution` | `superficial` | Safety None-pinning vs probe IR gates |
| submit_order side/shape helpers | `code_safety_ast` vs `ast_helpers` | mostly `superficial` | Different strictness / purpose |
| Intra-`quality_gates` imports | `ast_helpers` ← `code_safety` / `code_safety_ast` | already shared | Not duplication — note in matrix as `already-shared` |

**Abort path:** If Task 1 classifies every cross-file candidate as `already-shared` or `superficial`, skip Tasks 2–4 extraction, keep Task 5 as document-only close (PR body matrix + issue comment, no `ast_utils/` package).

---

### Task 1: Produce the audit matrix

**Files:**
- Read (inventory): the five in-scope modules above
- Produce (not committed long-term): paste-ready matrix text for the PR body (keep in the agent notes / PR draft only)

**Interfaces:**
- Consumes: helper inventories from the five files
- Produces: classification table with columns `primitive`, `locations`, `class` ∈ {`identical`,`unifiable`,`superficial`,`already-shared`}, `action`, `rationale`

- [ ] **Step 1: Inventory helpers**

From worktree root, run:

```bash
cd backend/agents && LLM_PROVIDER=dummy PYTHONPATH=..:. \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python <<'PY'
import ast, importlib, inspect
from pathlib import Path
files = {
  "code_safety": "investment_team.strategy_lab.quality_gates.code_safety",
  "code_safety_ast": "investment_team.strategy_lab.quality_gates.code_safety_ast",
  "ast_helpers": "investment_team.strategy_lab.quality_gates.code_conformance.ast_helpers",
  "predicate_resolution": "investment_team.strategy_lab.coverage_probe.predicate_resolution",
  "subcond_builder": "investment_team.strategy_lab.coverage_probe.subcond_builder",
}
for label, modname in files.items():
    m = importlib.import_module(modname)
    print(f"\n## {label}")
    for name, obj in sorted(vars(m).items()):
        if not callable(obj) or not name.startswith("_") or name.startswith("__"):
            continue
        if getattr(obj, "__module__", None) != m.__name__:
            print(f"  {name}  [re-export from {obj.__module__}]")
            continue
        try:
            src = inspect.getsource(obj)
        except OSError:
            continue
        first = (obj.__doc__ or "").strip().splitlines()[0] if obj.__doc__ else ""
        print(f"  {name}  lines={len(src.splitlines())}  {first[:90]}")
PY
```

Expected: printed inventory; mark `[re-export …]` rows as `already-shared` in the matrix (do not treat imports as duplication).

- [ ] **Step 2: Classify pairwise overlaps**

For every pair of *defined* (non-re-export) helpers that walk the same node kinds or share a name/purpose, open both source definitions and classify:

- `identical` — same contract and edge cases
- `unifiable` — contracts differ only by a wrapper (return `""` vs `None`, list vs first, case fold) and a shared core preserves both
- `superficial` — similar shape, divergent semantics (do **not** extract)
- `already-shared` — one module imports the other

Fill the matrix. At minimum explicitly classify the preliminary candidates table above.

- [ ] **Step 3: Decide extraction set**

Write the Action column:

- `extract-to-ast_utils` for `identical` / `unifiable`
- `leave` for `superficial` / `already-shared`

If the extraction set is empty → jump to Task 5 (document-only) and mark Tasks 2–4 cancelled in the PR.

- [ ] **Step 4: Commit checkpoint (optional notes only)**

Do **not** commit a long-lived audit markdown file. If you need a local scratch file while working, keep it untracked and delete before PR, or put the matrix only into the eventual `gh pr create` body.

No code commit required for Task 1 unless you prefer an empty checkpoint commit — prefer no commit.

---

### Task 2: `ast_utils.names` — shared name/attr core (TDD)

**Skip if** Task 1 did not mark `_get_call_name` / `_func_name` as `unifiable`/`identical`.

**Files:**
- Create: `backend/agents/investment_team/strategy_lab/ast_utils/__init__.py`
- Create: `backend/agents/investment_team/strategy_lab/ast_utils/names.py`
- Create: `backend/agents/investment_team/tests/test_strategy_lab_ast_utils.py`
- Modify: `backend/agents/investment_team/strategy_lab/quality_gates/code_safety_ast.py` (`_get_call_name`)
- Modify: `backend/agents/investment_team/strategy_lab/coverage_probe/subcond_builder.py` (`_func_name`)

**Interfaces:**
- Produces:
  - `name_or_attr(node: ast.AST | None) -> str | None`
  - `call_name(node: ast.Call) -> str`  # preserves `_get_call_name` contract (`""` when unknown, no case fold)
  - `func_name(func: ast.expr) -> str | None`  # preserves `_func_name` contract (lowercased, `None` when unknown)

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/investment_team/tests/test_strategy_lab_ast_utils.py`:

```python
"""Unit tests for strategy_lab.ast_utils shared AST primitives."""

from __future__ import annotations

import ast

import pytest

from investment_team.strategy_lab.ast_utils.names import call_name, func_name, name_or_attr


def _call(src: str) -> ast.Call:
    mod = ast.parse(src)
    stmt = mod.body[0]
    assert isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
    return stmt.value


class TestNameOrAttr:
    def test_name(self) -> None:
        assert name_or_attr(ast.Name(id="foo", ctx=ast.Load())) == "foo"

    def test_attribute(self) -> None:
        node = ast.Attribute(
            value=ast.Name(id="self", ctx=ast.Load()),
            attr="bar",
            ctx=ast.Load(),
        )
        assert name_or_attr(node) == "bar"

    def test_other_returns_none(self) -> None:
        assert name_or_attr(ast.Constant(value=1)) is None
        assert name_or_attr(None) is None


class TestCallNamePreservesSafetyContract:
    def test_simple_name(self) -> None:
        assert call_name(_call("foo()")) == "foo"

    def test_attribute(self) -> None:
        assert call_name(_call("ctx.submit_order()")) == "submit_order"

    def test_unknown_returns_empty_string(self) -> None:
        call = _call("(lambda: None)()")
        assert call_name(call) == ""


class TestFuncNamePreservesProbeContract:
    def test_lowercases_name(self) -> None:
        assert func_name(ast.Name(id="ATR", ctx=ast.Load())) == "atr"

    def test_lowercases_attribute(self) -> None:
        node = ast.Attribute(
            value=ast.Name(id="self", ctx=ast.Load()),
            attr="Bollinger",
            ctx=ast.Load(),
        )
        assert func_name(node) == "bollinger"

    def test_unknown_returns_none(self) -> None:
        assert func_name(ast.Constant(value="x")) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && LLM_PROVIDER=dummy PYTHONPATH=.:agents \
  .venv/bin/python -m pytest \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py -v
```

Use the parent repo venv if the worktree has none:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3051-ast-helper-audit/backend && \
LLM_PROVIDER=dummy PYTHONPATH=.:agents \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py -v
```

Expected: FAIL — `ModuleNotFoundError: investment_team.strategy_lab.ast_utils`

- [ ] **Step 3: Implement `ast_utils.names` + package init**

`backend/agents/investment_team/strategy_lab/ast_utils/names.py`:

```python
"""Shared AST name / attribute extraction helpers."""

from __future__ import annotations

import ast
from typing import Optional


def name_or_attr(node: Optional[ast.AST]) -> Optional[str]:
    """Return ``Name.id`` or ``Attribute.attr``, else ``None``.

    Preconditions:
      - ``node`` is an AST node or ``None``.
    Postconditions:
      - Returns the identifier string for ``ast.Name`` / ``ast.Attribute``.
      - Returns ``None`` for every other node type and for ``None``.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def call_name(node: ast.Call) -> str:
    """Extract the callable name from ``node`` without case folding.

    Preconditions:
      - ``node`` is an ``ast.Call``.
    Postconditions:
      - Returns ``Name.id`` or ``Attribute.attr`` of ``node.func``.
      - Returns ``""`` when ``node.func`` is neither (matches legacy
        ``code_safety_ast._get_call_name``).
    """
    return name_or_attr(node.func) or ""


def func_name(func: ast.expr) -> Optional[str]:
    """Extract and lowercase a callable expression name.

    Preconditions:
      - ``func`` is an ``ast.expr`` (typically ``Call.func``).
    Postconditions:
      - Returns lowercased ``Name.id`` / ``Attribute.attr``.
      - Returns ``None`` when neither (matches legacy
        ``coverage_probe.subcond_builder._func_name``).
    """
    raw = name_or_attr(func)
    return raw.lower() if raw is not None else None
```

`backend/agents/investment_team/strategy_lab/ast_utils/__init__.py`:

```python
"""Shared pure-AST helpers for Strategy Lab quality_gates and coverage_probe."""

from .names import call_name, func_name, name_or_attr

__all__ = ["call_name", "func_name", "name_or_attr"]
```

- [ ] **Step 4: Migrate call sites to wrappers**

In `code_safety_ast.py`, replace the body of `_get_call_name` with:

```python
def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node (handles simple names and attribute access)."""
    from investment_team.strategy_lab.ast_utils.names import call_name

    return call_name(node)
```

(Prefer a top-level import if it does not create a cycle; use the same style as neighboring imports.)

In `subcond_builder.py`, replace `_func_name` with:

```python
def _func_name(func: ast.expr) -> Optional[str]:
    from investment_team.strategy_lab.ast_utils.names import func_name as _shared_func_name

    return _shared_func_name(func)
```

Prefer top-level import at the bottom cross-import section only if needed to preserve the existing three-module cycle safety — otherwise top of file is fine for `ast_utils` (acyclic).

- [ ] **Step 5: Run unit + focused regression tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3051-ast-helper-audit/backend && \
LLM_PROVIDER=dummy PYTHONPATH=.:agents \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py \
  agents/investment_team/tests/test_indicator_probe_robustness.py \
  agents/investment_team/tests/test_code_safety.py \
  -q --tb=short
```

If `test_code_safety.py` does not exist, discover the real safety/conformance test modules:

```bash
ls agents/investment_team/tests/test_*code_safety* agents/investment_team/tests/test_*conformance* 2>/dev/null
```

Expected: PASS.

Because `subcond_builder.py` changed, also run branch coverage for the probe hot paths:

```bash
cd backend/agents && LLM_PROVIDER=dummy PYTHONPATH=..:. \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  investment_team/tests/ -m "not integration" \
  --cov=investment_team.strategy_lab.coverage_probe.subcond_builder \
  --cov-branch --cov-report=term-missing -q
```

Expected: no new partial-branch gaps introduced in `_func_name` callers; suite green.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/investment_team/strategy_lab/ast_utils/__init__.py \
  backend/agents/investment_team/strategy_lab/ast_utils/names.py \
  backend/agents/investment_team/tests/test_strategy_lab_ast_utils.py \
  backend/agents/investment_team/strategy_lab/quality_gates/code_safety_ast.py \
  backend/agents/investment_team/strategy_lab/coverage_probe/subcond_builder.py
git commit -m "$(cat <<'EOF'
Extract shared AST name helpers into strategy_lab.ast_utils.

Preserve call_name and func_name contracts at existing call sites so
quality-gate and coverage-probe behavior stays identical.
EOF
)"
```

---

### Task 3: `ast_utils.strategy_shape` — Strategy subclass + on_bar on class (TDD)

**Skip if** Task 1 rejected these candidates.

**Files:**
- Create: `backend/agents/investment_team/strategy_lab/ast_utils/strategy_shape.py`
- Modify: `backend/agents/investment_team/strategy_lab/ast_utils/__init__.py`
- Modify: `backend/agents/investment_team/tests/test_strategy_lab_ast_utils.py`
- Modify: `backend/agents/investment_team/strategy_lab/quality_gates/code_safety_ast.py`
- Modify (bonus): `backend/agents/investment_team/strategy_lab/coverage_probe/runtime_instrument.py`
- Modify (bonus): `backend/agents/investment_team/strategy_lab/quality_gates/universe_injection.py`

**Interfaces:**
- Produces:
  - `is_strategy_subclass(cls: ast.ClassDef) -> bool`
  - `iter_on_bar_methods(cls: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]`
  - `find_on_bar_method(cls: ast.ClassDef) -> ast.FunctionDef | ast.AsyncFunctionDef | None`  # first match

Do **not** replace `predicate_resolution._find_on_bar` (tree walk + entry/signal fallbacks) unless Task 1 explicitly marked a safe common core; default is leave it.

- [ ] **Step 1: Write failing tests**

Append to `test_strategy_lab_ast_utils.py`:

```python
from investment_team.strategy_lab.ast_utils.strategy_shape import (
    find_on_bar_method,
    is_strategy_subclass,
    iter_on_bar_methods,
)


def _module(src: str) -> ast.Module:
    return ast.parse(src)


class TestStrategySubclass:
    def test_bare_strategy_base(self) -> None:
        cls = _module("class S(Strategy):\n    pass").body[0]
        assert isinstance(cls, ast.ClassDef)
        assert is_strategy_subclass(cls) is True

    def test_contract_strategy_base(self) -> None:
        cls = _module("class S(contract.Strategy):\n    pass").body[0]
        assert isinstance(cls, ast.ClassDef)
        assert is_strategy_subclass(cls) is True

    def test_unrelated_base(self) -> None:
        cls = _module("class S(object):\n    pass").body[0]
        assert isinstance(cls, ast.ClassDef)
        assert is_strategy_subclass(cls) is False


class TestOnBarMethods:
    def test_finds_sync_and_async(self) -> None:
        cls = _module(
            "class S(Strategy):\n"
            "    def on_bar(self, ctx, bar):\n"
            "        return\n"
            "    async def on_bar(self, ctx, bar):\n"
            "        return\n"
        ).body[0]
        assert isinstance(cls, ast.ClassDef)
        methods = iter_on_bar_methods(cls)
        assert [type(m) for m in methods] == [ast.FunctionDef, ast.AsyncFunctionDef]
        assert find_on_bar_method(cls) is methods[0]

    def test_missing(self) -> None:
        cls = _module("class S(Strategy):\n    pass").body[0]
        assert isinstance(cls, ast.ClassDef)
        assert iter_on_bar_methods(cls) == []
        assert find_on_bar_method(cls) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3051-ast-helper-audit/backend && \
LLM_PROVIDER=dummy PYTHONPATH=.:agents \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py::TestStrategySubclass \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py::TestOnBarMethods -v
```

Expected: FAIL — import error for `strategy_shape`.

- [ ] **Step 3: Implement `strategy_shape.py`**

```python
"""Shared Strategy-class shape helpers."""

from __future__ import annotations

import ast
from typing import List, Optional, Union

OnBarMethod = Union[ast.FunctionDef, ast.AsyncFunctionDef]


def is_strategy_subclass(cls: ast.ClassDef) -> bool:
    """True iff ``cls`` syntactically subclasses ``Strategy`` or ``contract.Strategy``.

    Preconditions:
      - ``cls`` is a class definition AST node.
    Postconditions:
      - Matches bare ``Strategy`` and ``contract.Strategy`` bases only.
      - Does not resolve imports or indirect bases.
    """
    for base in cls.bases:
        if isinstance(base, ast.Name) and base.id == "Strategy":
            return True
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "Strategy"
            and isinstance(base.value, ast.Name)
            and base.value.id == "contract"
        ):
            return True
    return False


def iter_on_bar_methods(cls: ast.ClassDef) -> List[OnBarMethod]:
    """Return every direct ``on_bar`` method on ``cls`` (sync or async), in body order.

    Preconditions:
      - ``cls`` is a class definition AST node.
    Postconditions:
      - Only methods whose name is exactly ``on_bar``; nested defs ignored.
    """
    return [
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "on_bar"
    ]


def find_on_bar_method(cls: ast.ClassDef) -> Optional[OnBarMethod]:
    """Return the first direct ``on_bar`` method on ``cls``, or ``None``.

    Preconditions:
      - ``cls`` is a class definition AST node.
    Postconditions:
      - Equivalent to ``iter_on_bar_methods(cls)[0]`` when non-empty.
    """
    methods = iter_on_bar_methods(cls)
    return methods[0] if methods else None
```

Update `__init__.py` `__all__` to export the new symbols.

- [ ] **Step 4: Migrate call sites**

`code_safety_ast._find_on_bar_method` → delegate to `find_on_bar_method`.

`code_safety_ast._find_strategy_subclasses` → keep module-level filtering, but use `is_strategy_subclass` for the base check:

```python
def _find_strategy_subclasses(tree: ast.AST) -> List[ast.ClassDef]:
    from investment_team.strategy_lab.ast_utils.strategy_shape import is_strategy_subclass

    out: List[ast.ClassDef] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and is_strategy_subclass(node):
            out.append(node)
    return out
```

Bonus — `runtime_instrument._is_strategy_subclass` → import/alias shared helper; `_find_on_bar` may use `is_strategy_subclass` + `find_on_bar_method` but must preserve “sync `FunctionDef` only” if that is current behavior (do not start returning `AsyncFunctionDef` if the old helper ignored async).

Bonus — `universe_injection._find_on_bar_methods` → delegate to `iter_on_bar_methods`.

- [ ] **Step 5: Run tests**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3051-ast-helper-audit/backend && \
LLM_PROVIDER=dummy PYTHONPATH=.:agents \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py \
  agents/investment_team/tests/test_universe_injection.py \
  agents/investment_team/tests/test_strategy_lab_runtime_instrument.py \
  agents/investment_team/tests/test_streaming_harness_coverage_probe.py \
  -q --tb=short
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/investment_team/strategy_lab/ast_utils/strategy_shape.py \
  backend/agents/investment_team/strategy_lab/ast_utils/__init__.py \
  backend/agents/investment_team/tests/test_strategy_lab_ast_utils.py \
  backend/agents/investment_team/strategy_lab/quality_gates/code_safety_ast.py \
  backend/agents/investment_team/strategy_lab/coverage_probe/runtime_instrument.py \
  backend/agents/investment_team/strategy_lab/quality_gates/universe_injection.py
git commit -m "$(cat <<'EOF'
Centralize Strategy subclass and on_bar discovery in ast_utils.

Reuse one syntactic Strategy-base check and class-scoped on_bar finder
across safety, runtime instrumentation, and universe injection.
EOF
)"
```

---

### Task 4: Optional scopes helper + remaining Task-1 extract set

**Skip entirely if** Task 1 extraction set is empty after Tasks 2–3, or only listed the items already done.

**Files:**
- Create only if needed: `backend/agents/investment_team/strategy_lab/ast_utils/scopes.py`
- Modify callers listed in the Task 1 Action column

**Interfaces:**
- Only symbols Task 1 marked `extract-to-ast_utils` and not already handled
- Likely candidate if confirmed: move `_iter_method_body_nodes` implementation into `scopes.py` and re-export from `code_safety_ast` / import from `ast_helpers`

- [ ] **Step 1: For each remaining extract row, write a failing unit test** that locks the exact current contract (copy examples from existing callers / docstrings).

- [ ] **Step 2: Run the new tests — expect FAIL.**

- [ ] **Step 3: Implement the shared helper; replace local bodies with delegates.**

- [ ] **Step 4: Run `test_strategy_lab_ast_utils.py` plus the owning gate/probe suite — expect PASS.**

- [ ] **Step 5: Commit** with a message describing the primitive moved, not the issue number.

If Task 1 found no further rows, record “Task 4 skipped — no remaining extract set” in the PR body and do not create empty modules.

---

### Task 5: Full verification + PR documentation

**Files:**
- None required beyond what Tasks 2–4 changed
- PR body / issue comment = acceptance audit artifact

- [ ] **Step 1: Run quality-gate + coverage-probe focused suites**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3051-ast-helper-audit/backend && \
LLM_PROVIDER=dummy PYTHONPATH=.:agents \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py \
  agents/investment_team/tests/test_indicator_probe_robustness.py \
  agents/investment_team/tests/test_universe_injection.py \
  agents/investment_team/tests/test_strategy_lab_runtime_instrument.py \
  agents/investment_team/tests/test_strategy_lab_static_probe.py \
  -q --tb=short
```

Also run any `test_*code_safety*` / `test_*conformance*` / `test_*predicate*` modules discovered under `agents/investment_team/tests/`.

Expected: PASS; no assertion changes required.

- [ ] **Step 2: If `subcond_builder.py` or `subcondition_visitor.py` changed, re-check branch coverage**

Follow `strategy_lab/README.md` “Branch coverage for coverage_probe” section. Expected: no new uncovered arcs in the touched hot paths attributable to this refactor.

- [ ] **Step 3: Ruff**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3051-ast-helper-audit/backend && \
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check \
  agents/investment_team/strategy_lab/ast_utils \
  agents/investment_team/strategy_lab/quality_gates/code_safety_ast.py \
  agents/investment_team/strategy_lab/coverage_probe/subcond_builder.py \
  agents/investment_team/tests/test_strategy_lab_ast_utils.py
```

Expected: clean (or only pre-existing unrelated issues outside touched files).

- [ ] **Step 4: Open / update PR with audit matrix**

PR body must include:

1. Full audit matrix from Task 1
2. What was extracted vs left local and why
3. Test plan checklist
4. `Closes #3051`

Also post a short issue comment summarizing the outcome (consolidation done / document-only close).

- [ ] **Step 5: Final commit if any doc/plan tweaks remain**

```bash
git add docs/superpowers/plans/2026-08-06-strategy-lab-ast-utils-audit.md
git commit -m "$(cat <<'EOF'
Add implementation plan for Strategy Lab AST helper audit.

EOF
)"
```

(Only if this plan file is not yet committed.)

---

## Self-review (plan author)

1. **Spec coverage:** Goal, `ast_utils/` home, audit-first extract, PR-only documentation, no realism-gate / probe-semantics changes, ≥90% coverage, branch-coverage check when probe files change — all mapped to tasks.
2. **Placeholders:** No TBD extraction list — preliminary candidates + abort path + Task 4 residual hook.
3. **Type consistency:** `name_or_attr` / `call_name` / `func_name` / `is_strategy_subclass` / `iter_on_bar_methods` / `find_on_bar_method` names match across tasks.
