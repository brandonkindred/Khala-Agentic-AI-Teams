# Type `_PersistentDict.values()` as `List[Any]` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Annotate `_PersistentDict.values()` as `-> List[Any]` so static analysis sees the element type, matching the rest of `investment_team.api.main`.

**Architecture:** Annotation-only change on one method. Add a small `typing.get_type_hints` assertion next to the existing `_PersistentDict` coverage in `test_api_main_extra.py`. No runtime behavior change; `List`/`Any` are already imported in `main.py`.

**Tech Stack:** Python 3.10, `typing.List` / `typing.Any` / `typing.get_type_hints`, pytest, ruff via `backend/Makefile`.

## Global Constraints

- Behavior-preserving: do not change what `values()` returns at runtime.
- Do not audit or retype other `_PersistentDict` methods.
- Do not mention GitHub issue numbers in source, comments, or commit messages (PR body only: `Closes #1906`).
- Work in the existing worktree at `.worktrees/fix-1906-persistent-dict-values-typing` on branch `fix/1906-persistent-dict-values-typing`.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/investment_team/api/main.py` | Change `values()` return annotation from `list` to `List[Any]` (line ~258) |
| `backend/agents/investment_team/tests/test_api_main_extra.py` | Add `get_type_hints` assertion that the return annotation is `List[Any]` |

---

### Task 1: Lock the annotation with a failing test, then fix it

**Files:**
- Modify: `backend/agents/investment_team/tests/test_api_main_extra.py` (after the existing `_PersistentDict` roundtrip test block that ends around the `vals = pd.values()` assertions)
- Modify: `backend/agents/investment_team/api/main.py:258`

**Interfaces:**
- Consumes: `_PersistentDict.values` as defined on `investment_team.api.main._PersistentDict`
- Produces: `def values(self) -> List[Any]:` (annotation only)

- [ ] **Step 1: Write the failing annotation test**

Append this test to `backend/agents/investment_team/tests/test_api_main_extra.py` (near the other `_PersistentDict` tests; `List`/`Any` are already imported at the top of that file):

```python
def test_persistent_dict_values_return_annotation() -> None:
    """_PersistentDict.values must advertise List[Any] for static analysis."""
    from typing import get_type_hints

    from investment_team.api.main import _PersistentDict

    hints = get_type_hints(_PersistentDict.values)
    assert hints["return"] == List[Any]
```

- [ ] **Step 2: Run the test and confirm it fails**

Run from `backend/`:

```bash
LLM_PROVIDER=dummy python -m pytest agents/investment_team/tests/test_api_main_extra.py::test_persistent_dict_values_return_annotation -v
```

Expected: FAIL — assertion fails because the resolved return hint is bare `list`, not `List[Any]`.

- [ ] **Step 3: Change the annotation**

In `backend/agents/investment_team/api/main.py`, replace:

```python
    def values(self) -> list:
```

with:

```python
    def values(self) -> List[Any]:
```

Do not change the method body. Do not add imports (`List` and `Any` are already imported on the existing `from typing import ...` line).

- [ ] **Step 4: Re-run the annotation test**

```bash
LLM_PROVIDER=dummy python -m pytest agents/investment_team/tests/test_api_main_extra.py::test_persistent_dict_values_return_annotation -v
```

Expected: PASS

- [ ] **Step 5: Run existing `_PersistentDict` behavioral coverage**

```bash
LLM_PROVIDER=dummy python -m pytest agents/investment_team/tests/test_api_main_extra.py -k PersistentDict -v
```

Expected: PASS (runtime of `values()` unchanged)

- [ ] **Step 6: Full lint + test gate**

```bash
cd backend && LLM_PROVIDER=dummy make lint && LLM_PROVIDER=dummy make test
```

Expected: lint clean; test suite passes. No new ruff/mypy warnings attributable to this change.

- [ ] **Step 7: Commit**

```bash
git add backend/agents/investment_team/api/main.py \
        backend/agents/investment_team/tests/test_api_main_extra.py
git commit -m "$(cat <<'EOF'
Type _PersistentDict.values() return as List[Any].

EOF
)"
```
