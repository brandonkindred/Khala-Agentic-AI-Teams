# Remove code_review pytest Temporal Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the `"pytest" in sys.modules` backdoor from `code_review_temporal_enabled()` so pytest import alone no longer disables Temporal dispatch.

**Architecture:** Minimal surgical change in the code-review Temporal config gate: delete the pytest branch and unused `sys` import, rewrite the one contract test that asserted the old behavior, and update only docs/docstrings that claimed pytest disables Temporal. The `LLM_PROVIDER=dummy` disable and `CODE_REVIEW_TEMPORAL_FORCE` override stay until sibling work removes them.

**Tech Stack:** Python 3.10, pytest, existing `code_review_agent.temporal.config` module.

**Spec:** `docs/superpowers/specs/2026-08-07-remove-code-review-pytest-temporal-guard-design.md`

**Worktree:** `.worktrees/4001-remove-pytest-temporal-guard` on branch `feature/4001-remove-pytest-temporal-guard`

## Global Constraints

- Do not remove the `LLM_PROVIDER=dummy` Temporal-disable branch (sibling leaf).
- Do not convert broader `CodeReviewAgent.run()` tests (sibling leaf).
- Do not reword incidental pytest mentions in `worker.py` / `agent.py`.
- Do not reference GitHub issue numbers in code, comments, commit messages, or docs.
- Every public function touched must keep explicit Preconditions/Postconditions/Invariants in its docstring (Design by Contract).
- Prefer exact, minimal diffs; no drive-by refactors.

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/temporal/config.py` | Production enablement gate; delete pytest branch + `import sys`; update docstrings |
| `backend/agents/software_engineering_team/tests/test_code_review_temporal.py` | Enablement-gate contract tests; rewrite the pytest-default test |
| `docs/ENV_VARS.md` | Operator docs for `TEMPORAL_ADDRESS` (code review) and `CODE_REVIEW_TEMPORAL_FORCE` |

No new files.

---

### Task 1: Failing contract test + remove pytest gate

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_temporal.py` (enablement section around the current `test_enabled_is_false_under_pytest_by_default`)
- Modify: `backend/agents/software_engineering_team/code_review_agent/temporal/config.py`
- Test: `backend/agents/software_engineering_team/tests/test_code_review_temporal.py`

**Interfaces:**
- Consumes: `code_review_agent.temporal.config.code_review_temporal_enabled() -> bool`, existing `_clear_env(monkeypatch)` helper in the test file
- Produces: Gate that never inspects `sys.modules`; contract test asserting enabled under pytest when env is cleared

- [ ] **Step 1: Rewrite the contract test to expect the new behavior (TDD — fails first)**

In `backend/agents/software_engineering_team/tests/test_code_review_temporal.py`, replace:

```python
def test_enabled_is_false_under_pytest_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite runs under pytest, so the guard keeps reviews in-process.
    _clear_env(monkeypatch)
    assert cfg.code_review_temporal_enabled() is False
```

with:

```python
def test_enabled_is_true_under_pytest_when_env_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pytest alone must not disable Temporal; with env cleared the default
    # address resolves and the gate returns True.
    _clear_env(monkeypatch)
    assert cfg.code_review_temporal_enabled() is True
```

Leave `test_force_flag_enables_under_pytest`, `test_force_flag_still_requires_an_address`, and `test_dummy_harness_disables` unchanged.

- [ ] **Step 2: Run the new test to verify it fails**

From `backend/`:

```bash
.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py::test_enabled_is_true_under_pytest_when_env_cleared \
  -v
```

Expected: FAIL — assertion `False is True` (old pytest branch still returns `False`). If the renamed test is not collected because the old name remains, delete/rename first then re-run.

- [ ] **Step 3: Remove the pytest branch and unused import; update config docstrings**

In `backend/agents/software_engineering_team/code_review_agent/temporal/config.py`:

1. Change the module docstring opening paragraph so the disable list is only disable-sentinel `TEMPORAL_ADDRESS` values and the `dummy` LLM harness — remove “or running under `pytest`”.

Exact replacement for lines 8–11 (keep surrounding sentences):

```python
# ... When
# nothing is configured it targets the application's own deployed Temporal
# container (``temporal:7233`` — the address the docker stack already wires into
# every service), and an operator overrides that by pointing ``TEMPORAL_ADDRESS``
# at a different Temporal server. Setting ``TEMPORAL_ADDRESS`` to an empty /
# ``disabled`` / ``none`` / ``off`` value, or selecting the ``dummy`` LLM harness,
# falls the agent back to the in-process thread-mode coordinator.
```

2. Drop `import sys` (keep `import os` and `from typing import Optional`).

3. Update `_force_enabled` docstring:

```python
def _force_enabled() -> bool:
    """Test hook: ``CODE_REVIEW_TEMPORAL_FORCE`` in a truthy spelling.

    Lets an integration test opt back into Temporal mode despite the ``dummy``
    harness guard below. Never load-bearing outside tests.
    """
    return os.environ.get("CODE_REVIEW_TEMPORAL_FORCE", "").strip().lower() in _TRUE_VALUES
```

4. Update `code_review_temporal_enabled` docstring and body:

```python
def code_review_temporal_enabled() -> bool:
    """Whether ``CodeReviewAgent.run`` should dispatch to Temporal by default.

    Postconditions:
        - Returns ``True`` iff a Temporal address resolves and no disabling
          condition applies. The disabling condition is the ``dummy`` LLM
          harness — overridable by the ``CODE_REVIEW_TEMPORAL_FORCE`` test hook.
        - Never returns ``True`` when :func:`resolve_code_review_temporal_address`
          is ``None``.
        - Never raises.
        - Never inspects ``sys.modules``.
    """
    if _force_enabled():
        return resolve_code_review_temporal_address() is not None
    if _dummy_harness():
        return False
    return resolve_code_review_temporal_address() is not None
```

- [ ] **Step 4: Run enablement-gate tests to verify they pass**

From `backend/`:

```bash
.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py \
  -k "resolve_defaults or resolve_honours or disable_sentinels or enabled_is_true_under_pytest or force_flag or dummy_harness_disables" \
  -v
```

Expected: all selected tests PASS.

Also confirm no remaining pytest-module inspection:

```bash
rg -n 'pytest.*sys\.modules|sys\.modules.*pytest|"pytest" in sys\.modules' \
  agents/software_engineering_team/code_review_agent/
```

Expected: no hits in `temporal/config.py` (other files may mention pytest for unrelated reasons).

```bash
rg -n 'sys\.modules' \
  agents/software_engineering_team/code_review_agent/temporal/config.py
```

Expected: no hits.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/temporal/config.py \
  backend/agents/software_engineering_team/tests/test_code_review_temporal.py
git commit -m "$(cat <<'EOF'
Remove pytest-detection guard from code-review Temporal enablement.

EOF
)"
```

---

### Task 2: Update ENV_VARS docs

**Files:**
- Modify: `docs/ENV_VARS.md` (sections `TEMPORAL_ADDRESS (code review agent default)` and `CODE_REVIEW_TEMPORAL_FORCE`)
- Test: docs-only; re-run the same enablement-gate pytest filter from Task 1 as a smoke check

**Interfaces:**
- Consumes: Post-Task-1 gate behavior (dummy disable remains; pytest no longer disables)
- Produces: Operator docs that match the gate

- [ ] **Step 1: Edit the TEMPORAL_ADDRESS code-review paragraph**

In `docs/ENV_VARS.md`, find the sentence ending with the pytest clause and change:

```markdown
Setting `TEMPORAL_ADDRESS` to an empty /
`disabled` / `none` / `off` / `0` / `false` / `no` value, selecting
`LLM_PROVIDER=dummy`, or running under `pytest` falls back to thread mode.
```

to:

```markdown
Setting `TEMPORAL_ADDRESS` to an empty /
`disabled` / `none` / `off` / `0` / `false` / `no` value, or selecting
`LLM_PROVIDER=dummy`, falls back to thread mode.
```

- [ ] **Step 2: Edit the CODE_REVIEW_TEMPORAL_FORCE paragraph**

Change:

```markdown
### CODE_REVIEW_TEMPORAL_FORCE
Test-only escape hatch (truthy: `1`/`true`/`yes`/`on`) that re-enables code-review
Temporal mode despite the `pytest`/`dummy` guards, provided an address still
resolves. Not load-bearing outside integration tests.
```

to:

```markdown
### CODE_REVIEW_TEMPORAL_FORCE
Test-only escape hatch (truthy: `1`/`true`/`yes`/`on`) that re-enables code-review
Temporal mode despite the `dummy` guard, provided an address still resolves. Not
load-bearing outside integration tests.
```

- [ ] **Step 3: Smoke-check enablement tests still pass**

From `backend/`:

```bash
.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_temporal.py \
  -k "enabled_is_true_under_pytest or force_flag or dummy_harness_disables" \
  -v
```

Expected: all selected tests PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/ENV_VARS.md
git commit -m "$(cat <<'EOF'
Drop pytest from code-review Temporal env-var docs.

EOF
)"
```

---

## Self-Review

1. **Spec coverage:** Production gate deletion → Task 1 Step 3. Unused `sys` import → Task 1 Step 3. Contract test rewrite → Task 1 Step 1. ENV_VARS updates → Task 2. Verification greps → Task 1 Step 4. Out-of-scope leaves explicitly excluded in Global Constraints.
2. **Placeholder scan:** No TBD/TODO; exact code and commands included.
3. **Type consistency:** `code_review_temporal_enabled() -> bool` unchanged; test uses existing `cfg` import alias and `_clear_env`.
