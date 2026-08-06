# Fix Vacuous Temporal Package `os.getenv` Import Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `test_temporal_package_init_does_not_call_os_getenv` actually detect `os.getenv` calls during `investment_team.temporal.__init__` by removing the vacuous reset/re-import sequence.

**Architecture:** Test-only restructure. Purge the package tree, patch `os.getenv` with wraps, import `investment_team.temporal` once under the spy, assert zero calls. Mirror the sibling bootstrap test that already uses purge → import under patch without `reset_mock`.

**Tech Stack:** Python 3.10+, pytest, `unittest.mock`, `importlib`, existing `_purge` helper

## Global Constraints

- Modify only `backend/agents/investment_team/tests/test_temporal_bootstrap.py`
- Do not change `investment_team.temporal.__init__` (or other production code)
- Do not call `spy.reset_mock()` in this test
- Do not pre-import `workflows` before asserting on the package `__init__` path
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Worktree: `.worktrees/fix-4993-temporal-init-getenv-spy` on branch `fix/4993-temporal-init-getenv-spy`

## File map

| File | Role |
|---|---|
| `backend/agents/investment_team/tests/test_temporal_bootstrap.py` | Restructure `test_temporal_package_init_does_not_call_os_getenv` (~118–131); `_purge` already correct |
| `backend/agents/investment_team/temporal/__init__.py` | Reference only — must remain free of module-level `os.getenv`; do not edit |

---

### Task 1: Restructure the getenv spy test

**Files:**
- Modify: `backend/agents/investment_team/tests/test_temporal_bootstrap.py` (`test_temporal_package_init_does_not_call_os_getenv`)
- Reference (do not modify): `backend/agents/investment_team/temporal/__init__.py`
- Reference pattern: same file, `test_importing_temporal_package_does_not_call_start_team_worker` (~101–115)

**Interfaces:**
- Consumes: existing `_purge(prefix: str) -> None` (deletes `prefix` and `prefix.*` from `sys.modules`)
- Produces: non-vacuous assertion that package `__init__` made zero `os.getenv` calls

- [ ] **Step 1: Replace the vacuous test body with the purge → spy → package-import sequence**

Replace the entire body of `test_temporal_package_init_does_not_call_os_getenv` so it reads:

```python
def test_temporal_package_init_does_not_call_os_getenv() -> None:
    """The temporalio sandbox replays the package __init__ during workflow
    registration; any ``os.getenv`` there aborts registration."""
    import os

    _purge("investment_team.temporal")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("investment_team.temporal")
        assert spy.call_count == 0, (
            f"investment_team.temporal.__init__ called os.getenv {spy.call_count} "
            "time(s) at import — this trips the temporalio workflow sandbox."
        )
```

Ensure there is no `spy.reset_mock()`, and no import of `investment_team.temporal.workflows` inside this test.

- [ ] **Step 2: Run the test and confirm it passes against the current compliant `__init__`**

From `backend/` in the worktree (reuse main venv if worktree has none):

```bash
PYTHONPATH=".:agents" /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/pytest \
  agents/investment_team/tests/test_temporal_bootstrap.py::test_temporal_package_init_does_not_call_os_getenv -q
```

Expected: `1 passed`

- [ ] **Step 3: Optional mutation sanity check (do not commit)**

Temporarily add a line such as `import os; os.getenv("TEMPORAL_ADDRESS")` near the top of
`backend/agents/investment_team/temporal/__init__.py`, re-run the same pytest command,
expect FAIL with the assertion message mentioning `os.getenv`, then revert the temporary
line so production code is unchanged.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/investment_team/tests/test_temporal_bootstrap.py
git commit -m "$(cat <<'EOF'
Fix vacuous Temporal package os.getenv import-time spy test.

Purge and import investment_team.temporal under the spy without reset_mock
so package __init__ execution is actually asserted.
EOF
)"
```
